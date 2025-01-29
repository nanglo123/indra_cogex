import json
from typing import List, Optional, Mapping, Tuple
import logging

from flask import Blueprint, render_template, request, jsonify, redirect, url_for
from flask_jwt_extended import jwt_required
from flask_wtf import FlaskForm
from indra.statements import get_all_descendants, Statement
from wtforms import StringField, SubmitField
from wtforms.fields.simple import BooleanField
from wtforms.validators import DataRequired

from indra_cogex.analysis.metabolite_analysis import parse_metabolites
from indra_cogex.apps.utils import render_statements
from indra_cogex.client import Neo4jClient, autoclient
from indra_cogex.client.queries import *
from indra_cogex.representation import norm_id

logger = logging.getLogger(__name__)

__all__ = ["search_blueprint"]

from indra_cogex.client.queries import enrich_statements

from indra_cogex.representation import indra_stmts_from_relations

search_blueprint = Blueprint("search", __name__, url_prefix="/search")


class SearchForm(FlaskForm):
    agent_name = StringField("Agent Name", validators=[DataRequired()])
    agent_role = StringField("Agent Role")
    other_agent = StringField("Other Agent")
    other_agent_role = StringField("Other Agent Role")
    source_type = StringField("Source Type")
    rel_type = StringField("Relationship Type")
    left_arrow = BooleanField("⇐")
    right_arrow = BooleanField("➔")
    both_arrow = BooleanField("⇔")
    paper_id = StringField("Paper ID")
    mesh_terms = StringField("MeSH Terms")
    submit = SubmitField("Search")


@search_blueprint.route("/", methods=['GET', 'POST'])
@jwt_required(optional=True)
def search():
    stmt_types = {c.__name__ for c in get_all_descendants(Statement)}
    stmt_types -= {"Influence", "Event", "Unresolved"}
    stmt_types_json = json.dumps(sorted(list(stmt_types)))

    form = SearchForm()

    # POST Request: Generate a sharable link with query parameters
    if form.validate_on_submit():
        query_params = {
            "agent": form.agent_name.data,
            "agent_tuple": request.form.get("agent_tuple"),
            "other_agent": form.other_agent.data,
            "other_agent_tuple": request.form.get("other_agent_tuple"),
            "source_type": form.source_type.data,
            "rel_types": json.loads(form.rel_type.data) if form.rel_type.data else None,
            "agent_role": form.agent_role.data,
            "other_role": form.other_agent_role.data,
            "paper_id": form.paper_id.data,
            "mesh_terms": form.mesh_terms.data,
            "mesh_tuple": request.form.get("mesh_tuple"),
        }
        query_params = {k: v for k, v in query_params.items() if v}
        return redirect(url_for("search.search", **query_params))

    # GET Request: Extract query parameters and fetch statements
    agent = request.args.get("agent")
    agent_tuple = request.args.get("agent_tuple")
    if agent_tuple:
        source_db, source_id = json.loads(agent_tuple)
        agent = (source_db, source_id)

    other_agent = request.args.get("other_agent")
    other_agent_tuple = request.args.get("other_agent_tuple")
    if other_agent_tuple:
        source_db, source_id = json.loads(other_agent_tuple)
        other_agent = (source_db, source_id)

    source_type = request.args.get("source_type")
    rel_types = request.args.getlist("rel_types")

    agent_role = request.args.get("agent_role")
    other_role = request.args.get("other_role")
    paper_id = request.args.get("paper_id")
    mesh_terms = request.args.get("mesh_terms")
    mesh_tuple = request.args.get("mesh_tuple")
    if mesh_tuple:
        source_db, source_id = json.loads(mesh_tuple)
        mesh_terms = (source_db, source_id)

    # Fetch and display statements
    if agent or other_agent or rel_types:
        statements, evidence_count = get_statements(
            agent=agent,
            agent_role=agent_role,
            other_agent=other_agent,
            other_role=other_role,
            stmt_sources=source_type,
            rel_types=rel_types,
            paper_term=paper_id,
            mesh_term=mesh_terms,
            limit=1000,
            evidence_limit=1000,
            return_evidence_counts=True,
        )
        return render_statements(stmts=statements, evidence_count=evidence_count)

    # Render the form page
    return render_template(
        "search/search_page.html",
        form=form,
        stmt_types_json=stmt_types_json,
    )


from flask import current_app


@search_blueprint.route("/gilda_ground", methods=["GET", "POST"])
@jwt_required(optional=True)
def gilda_ground_endpoint():
    data = request.get_json()
    current_app.logger.info(f"Received payload: {data}")
    agent_text = data.get("agent")
    if not agent_text:
        return {"error": "Agent text is required."}, 400

    try:
        gilda_list = gilda_ground(agent_text)
        return jsonify(gilda_list)
    except Exception as e:
        return {"error": str(e)}, 500


def gilda_ground(agent_text):
    try:
        from gilda.api import ground
        return [r.to_json() for r in ground(agent_text)]
    except ImportError:
        import requests
        res = requests.post('http://grounding.indra.bio/ground', json={'text': agent_text})
        return res.json()
    except Exception as e:
        return {"error": f"Grounding failed: {str(e)}"}


@autoclient()
def get_ora_statements(
    target_id: str,
    genes: List[str],
    minimum_belief: float = 0.0,
    minimum_evidence: Optional[int] = None,
    is_downstream: bool = False,
    *,
    client: Neo4jClient,
) -> Tuple[List[Statement], Mapping[int, int]]:
    """Get statements connecting input genes to target entity for ORA analysis.

    Parameters
    ----------
    target_id : str
        The ID of the target entity (e.g., 'GO:0006955', 'MESH:D007239')
    genes : List[str]
        List of gene IDs (e.g., ['HGNC:6019', 'HGNC:11876'])
    minimum_belief : float
        Minimum belief score for relationships
    minimum_evidence : Optional[int]
        Minimum number of evidences required for a statement to be included
    is_downstream : bool
        Whether this is a downstream analysis
    client : Neo4jClient
        The Neo4j client to use for querying

    Returns
    -------
    :
        A tuple containing:
        - List of INDRA statements representing the relationships
        - Dictionary mapping statement hashes to their evidence counts
    """
    # Normalize gene IDs
    normalized_genes = [norm_id('HGNC', gene.split(':')[1]) for gene in genes]
    print(f"DEBUG: Normalized genes: {normalized_genes[:5]}...")

    # Handle different entity types and their relationships
    namespace = target_id.split(':')[0].lower()
    id_part = target_id.split(':')[1]

    if namespace == 'mesh':
        normalized_target = f"mesh:{id_part}"
        rel_types = ["indra_rel", "has_indication"]
    elif namespace == 'fplx':
        normalized_target = f"fplx:{id_part}"
        rel_types = ["indra_rel", "isa"]
    else:
        normalized_target = target_id.lower()
        rel_types = ["indra_rel"]

    # Main query for getting statements
    query = """
    MATCH p = (d:BioEntity {id: $target_id})-[r]->(u:BioEntity)
    WHERE type(r) IN $rel_types
    AND u.id STARTS WITH "hgnc"
    AND NOT u.obsolete
    AND u.id IN $genes
    AND (type(r) <> 'indra_rel' OR r.belief > $minimum_belief)
    WITH distinct r.stmt_hash AS hash, collect(p) as pp
    RETURN pp
    """

    if is_downstream:
        query = """
        MATCH p = (u:BioEntity)-[r]->(d:BioEntity {id: $target_id})
        WHERE type(r) IN $rel_types
        AND u.id STARTS WITH "hgnc"
        AND NOT u.obsolete
        AND u.id IN $genes
        AND (type(r) <> 'indra_rel' OR r.belief > $minimum_belief)
        WITH distinct r.stmt_hash AS hash, collect(p) as pp
        RETURN pp
        """

    params = {
        "target_id": normalized_target,
        "genes": normalized_genes,
        "rel_types": rel_types,
        "minimum_belief": minimum_belief
    }
    results = client.query_tx(query, **params)
    flattened_rels = [client.neo4j_to_relation(i[0]) for rel in results for i in rel]

    # Filter relations based on minimum_evidence
    if minimum_evidence:
        flattened_rels = [
            rel for rel in flattened_rels
            if rel.data.get("evidence_count", 0) >= minimum_evidence
        ]

    stmts = indra_stmts_from_relations(flattened_rels, deduplicate=True)

    # Enrich statements with complete evidence (no limit)
    stmts = enrich_statements(
        stmts,
        client=client
    )

    # Create evidence count mapping
    evidence_counts = {
        stmt.get_hash(): rel.data.get("evidence_count", 0)
        for rel, stmt in zip(flattened_rels, stmts)
    }

    return stmts, evidence_counts


@search_blueprint.route("/ora_statements/", methods=['GET'])
@jwt_required(optional=True)
def search_ora_statements():
    """Endpoint to get INDRA statements connecting input genes to a target entity."""
    target_id = request.args.get("target_id")
    genes = request.args.getlist("genes")
    is_downstream = request.args.get("is_downstream", "").lower() == "true"

    try:
        minimum_evidence = int(request.args.get("minimum_evidence") or 2)
        minimum_belief = float(request.args.get("minimum_belief") or 0.0)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid parameter values"}), 400

    if not target_id or not genes:
        return jsonify({"error": "target_id and genes are required"}), 400

    try:
        statements, evidence_counts = get_ora_statements(
            target_id=target_id,
            genes=genes,
            minimum_belief=minimum_belief,
            minimum_evidence=minimum_evidence,
            is_downstream=is_downstream
        )

        return render_statements(
            stmts=statements,
            evidence_count=evidence_counts
        )

    except Exception as e:
        print(f"Error in get_ora_statements: {str(e)}")
        return jsonify({"error": str(e)}), 500


@autoclient()
def get_signed_statements(
    target_id: str,
    positive_genes: List[str],
    negative_genes: List[str],
    minimum_belief: float = 0.0,
    minimum_evidence: Optional[int] = None,
    *,
    client: Neo4jClient,
) -> Tuple[List[Statement], Mapping[int, int]]:
    """Get statements for signed analysis considering relationship direction."""
    # Normalize genes
    pos_genes = [norm_id('HGNC', gene.split(':')[1]) for gene in positive_genes]
    neg_genes = [norm_id('HGNC', gene.split(':')[1]) for gene in negative_genes]

    # Entity handling
    namespace = target_id.split(':')[0].lower()
    id_part = target_id.split(':')[1]

    if namespace == 'chebi':
        normalized_target = f"chebi:{id_part}"
        rel_types = ["indra_rel", "has_metabolite"]
    elif namespace == 'mesh':
        normalized_target = f"mesh:{id_part}"
        rel_types = ["indra_rel", "has_indication"]
    elif namespace == 'hgnc':
        normalized_target = f"hgnc:{id_part}"
        rel_types = ["indra_rel"]
    elif namespace == 'fplx':
        normalized_target = f"fplx:{id_part}"
        rel_types = ["indra_rel", "isa"]
    else:
        normalized_target = target_id.lower()
        rel_types = ["indra_rel"]

    # Modified query to return the path
    query = """
    MATCH p = (gene:BioEntity)-[r]-(target:BioEntity)
    WHERE target.id = $target_id
    AND type(r) IN $rel_types
    AND gene.id IN $gene_list
    AND r.belief > $minimum_belief
    AND r.evidence_count >= $minimum_evidence
    AND NOT gene.obsolete
    AND (
        // Gene->Target direction
        (startNode(r) = gene AND r.stmt_type IN $stmt_types_gene_to_target)
        OR
        // Target->Gene direction
        (startNode(r) = target AND r.stmt_type IN $stmt_types_target_to_gene)
    )
    RETURN p
    """

    flattened_rels = []

    # Process positive genes
    if pos_genes:
        pos_params = {
            "target_id": normalized_target,
            "gene_list": pos_genes,
            "rel_types": rel_types,
            "minimum_belief": minimum_belief,
            "minimum_evidence": minimum_evidence,
            # When gene acts on target
            "stmt_types_gene_to_target": [
                'DecreaseAmount',
                'Inhibition'
            ],
            # When target acts on gene
            "stmt_types_target_to_gene": [
                'IncreaseAmount',
                'Activation',
                'Complex'
            ]
        }
        logger.info("Executing positive genes query with params: %s", pos_params)
        pos_results = client.query_tx(query, **pos_params)
        for result in pos_results:
            path = result[0]  # Get the path from the result
            rel = client.neo4j_to_relation(path)
            flattened_rels.append(rel)
        logger.info(f"Found {len(flattened_rels)} positive relationships")

    # Process negative genes
    if neg_genes:
        neg_params = {
            "target_id": normalized_target,
            "gene_list": neg_genes,
            "rel_types": rel_types,
            "minimum_belief": minimum_belief,
            "minimum_evidence": minimum_evidence,
            # When gene acts on target
            "stmt_types_gene_to_target": [
                'IncreaseAmount',
                'Activation'
            ],
            # When target acts on gene
            "stmt_types_target_to_gene": [
                'DecreaseAmount',
                'Inhibition'
            ]
        }
        logger.info("Executing negative genes query with params: %s", neg_params)
        neg_results = client.query_tx(query, **neg_params)
        neg_count = 0
        for result in neg_results:
            path = result[0]  # Get the path from the result
            rel = client.neo4j_to_relation(path)
            flattened_rels.append(rel)
            neg_count += 1
        logger.info(f"Found {neg_count} negative relationships")

    logger.info(f"Total relationships found: {len(flattened_rels)}")

    # Create statements
    stmts = indra_stmts_from_relations(flattened_rels, deduplicate=True)
    logger.info(f"Created {len(stmts)} statements")

    # Enrich statements
    stmts = enrich_statements(stmts, client=client)
    logger.info("Completed statement enrichment")

    # Create evidence counts mapping
    evidence_counts = {
        stmt.get_hash(): rel.data.get("evidence_count", 0)
        for rel, stmt in zip(flattened_rels, stmts)
    }
    logger.info(f"Generated evidence counts: {evidence_counts}")

    return stmts, evidence_counts


@search_blueprint.route("/signed_statements/", methods=['GET'])
@jwt_required(optional=True)
def search_signed_statements():
    """Endpoint to get INDRA statements for signed analysis results."""
    # Log all request arguments
    logger.info("Received request arguments:")
    for key, value in request.args.items():
        logger.info(f"{key}: {value}")

    target_id = request.args.get("target_id")
    positive_genes = request.args.getlist("positive_genes")
    negative_genes = request.args.getlist("negative_genes")
    minimum_evidence = request.args.get("minimum_evidence", "1")
    minimum_belief = request.args.get("minimum_belief", "0.0")

    try:
        minimum_evidence = int(minimum_evidence)
        minimum_belief = float(minimum_belief)
    except (ValueError, TypeError) as e:
        logger.error(f"Parameter conversion error: {str(e)}")
        return jsonify({"error": "Invalid parameter values"}), 400

    if not target_id or (not positive_genes and not negative_genes):
        return jsonify({"error": "target_id and at least one gene list required"}), 400

    statements, evidence_counts = get_signed_statements(
        target_id=target_id,
        positive_genes=positive_genes,
        negative_genes=negative_genes,
        minimum_belief=minimum_belief,
        minimum_evidence=minimum_evidence
    )

    return render_statements(
        stmts=statements,
        evidence_count=evidence_counts
    )


@autoclient()
def get_metabolite_statements(
    target_id: str,
    metabolites: List[str],
    minimum_belief: float = 0.0,
    minimum_evidence: Optional[int] = None,
    *,
    client: Neo4jClient,
) -> Tuple[List[Statement], Mapping[int, int]]:
    """Get statements for metabolite analysis."""
    logger.info(f"\n{'=' * 80}\nStarting metabolite statement analysis")
    logger.info(f"Target: {target_id}")
    logger.info(f"Metabolites: {metabolites}")
    logger.info(f"Minimum belief: {minimum_belief}")
    logger.info(f"Minimum evidence: {minimum_evidence}\n")

    # Parse and normalize metabolite IDs
    chebi_ids, errors = parse_metabolites(metabolites)
    if errors:
        logger.warning(f"Could not parse the following metabolites: {errors}")

    # Add CHEBI prefix and log
    metabolite_list = [f"chebi:{m.lower()}" for m in chebi_ids]
    logger.info(f"Normalized CHEBI IDs: {metabolite_list}")

    # Add prefix for EC codes if needed
    normalized_target = f"eccode:{target_id}" if not ':' in target_id else target_id
    logger.info(f"Normalized target: {normalized_target}")

    # Discovery query to check what relationships exist
    discovery_query = """
    MATCH (met:BioEntity)-[r]-(target:BioEntity)
    WHERE met.id IN $metabolite_list
    RETURN DISTINCT
        met.id as metabolite,
        type(r) as rel_type,
        r.stmt_type as stmt_type,
        count(*) as count
    """

    logger.info("\nDiscovering relationships...")
    discovery_results = client.query_tx(discovery_query,
                                        metabolite_list=metabolite_list)
    for row in discovery_results:
        logger.info(f"Metabolite: {row[0]}, Type: {row[1]}, Statement Type: {row[2]}, Count: {row[3]}")

    # Main query for statements
    query = """
    MATCH p = (met:BioEntity)-[r:indra_rel]-(target:BioEntity)
    WHERE target.id = $target_id
    AND met.id IN $metabolite_list
    AND r.belief > $minimum_belief
    AND r.evidence_count >= $minimum_evidence
    AND NOT met.obsolete
    RETURN p
    """

    params = {
        "target_id": normalized_target,
        "metabolite_list": metabolite_list,
        "minimum_belief": minimum_belief,
        "minimum_evidence": minimum_evidence
    }

    logger.info("\nExecuting main query with params:")
    for key, value in params.items():
        logger.info(f"{key}: {value}")

    results = client.query_tx(query, **params)
    flattened_rels = []
    for result in results:
        path = result[0]
        rel = client.neo4j_to_relation(path)
        flattened_rels.append(rel)
        logger.debug(f"\nFound relationship:")
        logger.debug(f"Type: {rel.data.get('stmt_type')}")
        logger.debug(f"Evidence count: {rel.data.get('evidence_count')}")
        logger.debug(f"Belief: {rel.data.get('belief')}")

    logger.info(f"\nFound {len(flattened_rels)} total relationships")

    # Create statements
    stmts = indra_stmts_from_relations(flattened_rels, deduplicate=True)
    logger.info(f"Created {len(stmts)} statements")

    # Enrich statements
    stmts = enrich_statements(stmts, client=client)
    logger.info("Completed statement enrichment")

    # Create evidence counts mapping
    evidence_counts = {
        stmt.get_hash(): rel.data.get("evidence_count", 0)
        for rel, stmt in zip(flattened_rels, stmts)
    }
    logger.info(f"Evidence counts: {evidence_counts}")
    logger.info(f"{'=' * 80}\n")

    return stmts, evidence_counts


@search_blueprint.route("/metabolite_statements/", methods=['GET'])
@jwt_required(optional=True)
def search_metabolite_statements():
    """Endpoint to get INDRA statements for metabolite analysis results."""
    target_id = request.args.get("target_id")
    metabolites = request.args.getlist("metabolites")

    try:
        minimum_evidence = int(request.args.get("minimum_evidence", "1"))
        minimum_belief = float(request.args.get("minimum_belief", "0.0"))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid parameter values"}), 400

    if not target_id or not metabolites:
        return jsonify({"error": "target_id and metabolites are required"}), 400

    try:
        statements, evidence_counts = get_metabolite_statements(
            target_id=target_id,
            metabolites=metabolites,
            minimum_belief=minimum_belief,
            minimum_evidence=minimum_evidence,
        )

        return render_statements(
            stmts=statements,
            evidence_count=evidence_counts
        )

    except Exception as e:
        logger.error(f"Error in get_metabolite_statements: {str(e)}")
        return jsonify({"error": str(e)}), 400
