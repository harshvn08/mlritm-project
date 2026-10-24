import hashlib
import io
import json
import csv
from typing import List, Optional, Tuple, Dict, Any, Union
from fastapi import APIRouter, File, UploadFile, HTTPException, Header, Query, Body, status, Response, Form
from pydantic import ValidationError
from neo4j.exceptions import ServiceUnavailable
from backend.database import db
from backend.services.ingestion_engine import CaseIngestionEngine
from backend.services.schema_mapper import map_ingestion_to_graph_data
from backend.services.ingestion_service import IngestionService
from backend.models.case_input import CaseData, CaseEnvelope, CaseMetadata, IngestCreatedCounts, IngestResponse
from backend.logging_config import logger
from backend.config import settings
from backend.local_store import ingest as local_ingest, register_documents_present

router = APIRouter(prefix="/api/v1", tags=["Unified Data Ingestion"])

SUPPORTED_EXTENSIONS = {".csv", ".json", ".txt", ".pdf", ".xlsx", ".docx"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# (case_data, dataset_id, dataset_version): exactly what IngestionService.ingest_case needs.
CaseDocument = Tuple[CaseData, str, str]


def _has_extractable_content(engine: CaseIngestionEngine) -> bool:
    """Keep an ingestion request honest: a case is only persisted when evidence produced graph data."""
    return any((
        engine.people_by_id,
        engine.phones_by_msisdn,
        engine.accounts_by_num,
        engine.vehicles_by_plate,
        engine.social_by_handle,
        engine.communications,
        engine.transactions,
        engine.surveillance_logs,
        engine.intel_reports,
    ))


def _case_data_has_evidence(case_data: CaseData) -> bool:
    entities = case_data.entities
    relationships = case_data.relationships
    return any((
        entities.people, entities.phones, entities.bank_accounts, entities.vehicles, entities.social_handles,
        getattr(entities, "ip_addresses", []), getattr(entities, "locations", []), getattr(entities, "cell_towers", []),
        relationships.communications, relationships.transactions, case_data.surveillance_logs,
        case_data.intelligence_reports,
    ))


def _detect_case_data_document(filename: str, text_content: str) -> Optional[CaseDocument]:
    """Return a validated CaseData document if the uploaded content is a structured JSON case, else ``None``.

    Detection allows structured JSON files (whether CaseEnvelope, CaseData, or ConsolidatedCaseData format)
    to bypass the legacy unstructured extraction engine and proceed directly to graph generation:
      * the content must parse as a JSON object;
      * a CaseEnvelope-shaped object (``{"case_data": {...}}``) is unwrapped;
      * an object declaring ``case_metadata`` is validated as CaseData, or translated via ``map_ingestion_to_graph_data``;
      * non-JSON files or arbitrary JSON without ``case_metadata`` return ``None`` and go through the legacy extraction engine.
    """
    try:
        data = json.loads(text_content)
    except ValueError:  # includes json.JSONDecodeError
        return None
    if not isinstance(data, dict):
        return None

    document: Optional[CaseDocument] = None

    if isinstance(data.get("case_data"), dict):
        try:
            envelope = CaseEnvelope.model_validate(data)
            document = (
                envelope.case_data,
                envelope.dataset_id or "DS-DEFAULT",
                envelope.dataset_version or "1.0",
            )
        except ValidationError:
            pass

    if document is None and "case_metadata" in data:
        # First, attempt strict CaseData validation
        try:
            document = (CaseData.model_validate(data), "DS-DEFAULT", "1.0")
        except ValidationError:
            # Second, attempt direct schema mapping (supports ConsolidatedCaseData & other structured JSON formats)
            try:
                mapped = map_ingestion_to_graph_data(data)
                document = (mapped, "DS-DEFAULT", "1.0")
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File '{filename}' declares 'case_metadata' but could not be parsed or mapped into a valid CaseData document: {str(exc)}",
                )

    if document is None:
        return None

    if not document[0].case_metadata or not document[0].case_metadata.case_id:
        # Check if case_id can be derived or if it's missing
        if getattr(document[0].case_metadata, "fir_number", None):
            document[0].case_metadata.case_id = f"CASE_{document[0].case_metadata.fir_number.replace('/', '_')}"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{filename}': missing required 'case_metadata.case_id' in ingestion payload.",
            )
    return document



def _combine_results(results: List[IngestResponse]) -> IngestResponse:
    """One IngestResponse per request: unchanged for a single document, totals for several.

    Several documents occur when a folder upload contains more than one CaseData JSON file
    (legacy files are still consolidated into a single document, as before).  Counts are
    summed, insights are de-duplicated by ``insight_id``, ``case_id`` is the last case
    ingested and a warning lists every case ingested.
    """
    if len(results) == 1:
        return results[0]

    insights, seen_ids = [], set()
    for result in results:
        for insight in result.insights:
            if insight.insight_id not in seen_ids:
                seen_ids.add(insight.insight_id)
                insights.append(insight)

    warnings = [warning for result in results for warning in result.warnings]
    warnings.append(
        f"Ingested {len(results)} case documents in one request "
        f"({', '.join(result.case_id for result in results)}); counts are totals and "
        f"'case_id' is the last case ingested."
    )
    last = results[-1]
    return IngestResponse(
        case_id=last.case_id,
        dataset_id=last.dataset_id,
        dataset_version=last.dataset_version,
        created=IngestCreatedCounts(
            nodes=sum(result.created.nodes for result in results),
            relationships=sum(result.created.relationships for result in results),
            source_records=sum(result.created.source_records for result in results),
        ),
        matched_existing_entities=sum(result.matched_existing_entities for result in results),
        new_cross_case_links=sum(
            1 for insight in insights if insight.insight_type in ["CROSS_CASE_LINK", "SHARED_ENTITY", "BRIDGE_NODE"]
        ),
        new_insights=len(insights),
        warnings=warnings,
        insights=insights,
        converted_case_data=last.converted_case_data
    )


def _extract_text_from_file(filename: str, contents: bytes, content_type: Optional[str] = None) -> str:
    filename_lower = filename.lower()
    extension = "." + filename_lower.rsplit(".", 1)[-1] if "." in filename_lower else ""
    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"File '{filename}' is not supported. Upload CSV, JSON, TXT, PDF, XLSX, or DOCX.")
    if not contents:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"File '{filename}' is empty.")
    is_pdf = (
        filename_lower.endswith(".pdf")
        or (content_type and content_type.lower() == "application/pdf")
        or contents.startswith(b"%PDF-")
    )
    if is_pdf:
        extracted_pages = []
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(contents)) as pdf:
                for idx, page in enumerate(pdf.pages):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        extracted_pages.append(f"--- Page {idx + 1} ---\n{page_text}")
        except Exception as exc:
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(contents))
                for idx, page in enumerate(reader.pages):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        extracted_pages.append(f"--- Page {idx + 1} ---\n{page_text}")
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"File '{filename}' is a PDF but could not be parsed: {str(exc)}",
                )
        text_content = "\n\n".join(extracted_pages).strip()
        if not text_content:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"File '{filename}' is a PDF but contains no extractable text (it may be scanned, image-only, or encrypted).",
            )
        return text_content

    if extension == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="XLSX support requires the optional 'openpyxl' dependency.")
        try:
            workbook = load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
            sheet = workbook.active
            rows = [",".join("" if value is None else str(value) for value in row) for row in sheet.iter_rows(values_only=True)]
            text_content = "\n".join(rows).strip()
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"File '{filename}' could not be read as an XLSX workbook: {exc}")
        if not text_content:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"File '{filename}' contains no usable spreadsheet rows.")
        return text_content

    if extension == ".docx":
        try:
            from docx import Document
        except ImportError:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="DOCX support requires the optional 'python-docx' dependency.")
        try:
            document = Document(io.BytesIO(contents))
            text_content = "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()).strip()
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"File '{filename}' could not be read as a DOCX document: {exc}")
        if not text_content:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"File '{filename}' contains no extractable text.")
        return text_content

    # Evidence exported from call-detail systems is commonly UTF-8 with a BOM,
    # UTF-16, or Windows-1252. Decode strictly in order so malformed byte
    # sequences do not silently corrupt CSV column names or identifiers.
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return contents.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"File '{filename}' has an unsupported text encoding. Save it as UTF-8 CSV or TXT and retry.",
    )


@router.post("/ingest", response_model=IngestResponse, summary="Ingest case files (structured CSV & unstructured text) to Neo4j")
@router.post("/ingest/file", response_model=IngestResponse, summary="Ingest case files (structured CSV & unstructured text) to Neo4j alias")
async def ingest_files(
    response: Response,
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),
    case_id: Optional[str] = Query(None),
    case_title: Optional[str] = Form(None),
    uploaded_by: Optional[str] = Form(None),
    officer_name: Optional[str] = Form(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None)
):
    upload_files: List[UploadFile] = []
    if files:
        upload_files.extend(files)
    if file:
        upload_files.append(file)

    if not upload_files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    active_key = x_api_key or api_key
    # Local mode deliberately uses deterministic offline extraction, never an external LLM.
    engine = CaseIngestionEngine(api_key=None if settings.STORAGE_BACKEND.lower() == "sqlite" else active_key)
    if settings.STORAGE_BACKEND.lower() == "sqlite":
        engine.api_key = None
    if case_id:
        engine.case_metadata.case_id = case_id
        engine.case_metadata.case_name = case_title.strip() if case_title and case_title.strip() else f"Case {case_id}"
    if uploaded_by:
        engine.case_metadata.uploaded_by = uploaded_by
    if officer_name:
        engine.case_metadata.lead_investigator = officer_name

    documents: List[CaseDocument] = []
    legacy_files = 0
    seen_hashes: set = set()

    local_documents = []
    for upload_file in upload_files:
        filename = upload_file.filename or "uploaded_file.txt"
        contents = await upload_file.read()
        if len(contents) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"File '{filename}' exceeds the 20 MB upload limit.")
        file_hash = hashlib.sha256(contents).hexdigest()

        if file_hash in seen_hashes:
            logger.info(f"Skipping duplicate file payload '{filename}' (SHA256: {file_hash[:8]}).")
            continue
        seen_hashes.add(file_hash)

        text_content = _extract_text_from_file(filename, contents, upload_file.content_type)
        local_documents.append((filename, contents, text_content))

        # CaseData JSON documents take the same pipeline as POST /api/cases/ingest and never
        # enter the legacy CaseIngestionEngine; everything else is parsed exactly as before.
        document = _detect_case_data_document(filename, text_content)
        if document is not None:
            case_doc, ds_id, ds_ver = document
            if case_id:
                case_doc.case_metadata.case_id = case_id
            if case_title and case_title.strip():
                case_doc.case_metadata.case_name = case_title.strip()
            if uploaded_by:
                case_doc.case_metadata.uploaded_by = uploaded_by
            if officer_name:
                case_doc.case_metadata.lead_investigator = officer_name
            logger.info(
                f"'{filename}' is a CaseData JSON document (case '{case_doc.case_metadata.case_id}'); "
                f"bypassing legacy CaseIngestionEngine."
            )
            documents.append((case_doc, ds_id, ds_ver))
            continue

        # Explicit entity/relationship register CSVs are handled by local_store,
        # where row evidence and source-document IDs can be retained. They must
        # not be passed through the prose/person extraction fallback.
        is_register = register_documents_present([(filename, contents, text_content)])
        if not is_register:
            try:
                engine.parse_file(filename, text_content)
            except (csv.Error, UnicodeError, ValueError) as exc:
                logger.warning(f"Could not parse uploaded evidence '{filename}': {exc}")
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"File '{filename}' could not be parsed: {exc}",
                ) from exc
            legacy_files += 1

    if legacy_files:
        if not _has_extractable_content(engine) and not register_documents_present(local_documents):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="No usable entities or source-supported relationships were extracted. Check the CSV headers or provide a document containing identifiable people, phones, accounts, vehicles, or explicit associations.",
            )
        consolidated_case = engine.consolidate()
        graph_case_data = map_ingestion_to_graph_data(consolidated_case.model_dump())
        if case_id:
            graph_case_data.case_metadata.case_id = case_id
        if case_title and case_title.strip():
            graph_case_data.case_metadata.case_name = case_title.strip()
        if uploaded_by:
            graph_case_data.case_metadata.uploaded_by = uploaded_by
        if officer_name:
            graph_case_data.case_metadata.lead_investigator = officer_name
        documents.append((graph_case_data, "DS-DEFAULT", "1.0"))

    if not documents and settings.STORAGE_BACKEND.lower() == "sqlite" and register_documents_present(local_documents):
        if not case_id:
            case_id = "CASE_" + hashlib.sha256((case_title or "case" + str(len(local_documents))).encode()).hexdigest()[:12].upper()
        title = case_title.strip() if case_title and case_title.strip() else f"Case {case_id}"
        documents.append((CaseData(case_metadata=CaseMetadata(case_id=case_id, case_name=title)), "LOCAL", "1.0"))

    if not documents:
        raise HTTPException(status_code=400, detail="No valid unique documents were found to ingest.")




    if settings.STORAGE_BACKEND.lower() == "sqlite":
        if not case_id:
            case_id = "CASE_" + hashlib.sha256((case_title or "case" + str(len(local_documents))).encode()).hexdigest()[:12].upper()
        title = case_title.strip() if case_title and case_title.strip() else engine.case_metadata.case_name or f"Case {case_id}"
        if not _case_data_has_evidence(documents[0][0]) and not register_documents_present(local_documents):
            raise HTTPException(status_code=422, detail="No supported entities or relationships were extracted. Check document contents and CSV headers.")
        # Multiple files are consolidated into one case; retain parsed actual contents as source documents.
        combined_data = documents[0][0]
        counts, saved_data = local_ingest(case_id, title, combined_data, local_documents, uploaded_by)
        response.status_code = status.HTTP_201_CREATED
        return IngestResponse(case_id=case_id, dataset_id="LOCAL", dataset_version="1.0",
            created=IngestCreatedCounts(nodes=counts["nodes"], relationships=counts["relationships"], source_records=counts["source_records"]),
            warnings=["Extracted associations are evidence-based candidates for analyst verification."], converted_case_data=CaseData.model_validate(saved_data))

    try:
        with db.get_session() as session:
            results = []
            for doc_data, doc_ds_id, doc_ds_ver in documents:
                if not _case_data_has_evidence(doc_data):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Case '{doc_data.case_metadata.case_id}' contains no usable entities, relationships, or source evidence to ingest.",
                    )
                res = IngestionService.ingest_case(
                    session=session,
                    case_data=doc_data,
                    dataset_id=doc_ds_id,
                    dataset_version=doc_ds_ver,
                    mode="merge"
                )
                res.converted_case_data = doc_data
                results.append(res)
            response.status_code = status.HTTP_201_CREATED
            return _combine_results(results)
    except HTTPException:
        raise
    except ServiceUnavailable as exc:
        logger.warning(f"Neo4j is unavailable during file ingestion: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graph database is unavailable. Start Neo4j and retry the upload; no case data was saved.",
        )
    except Exception as e:
        logger.exception(f"Error saving case ingestion to Neo4j: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to ingest case data into Neo4j: {str(e)}"
        )


@router.post("/ingest/case", response_model=IngestResponse, summary="Ingest Case Graph JSON payload")
def ingest_case_json(
    response: Response,
    payload: Dict[str, Any] = Body(..., description="Case Envelope, CaseData, or structured Case JSON payload"),
    mode: str = Query("merge", description="Ingestion mode: 'merge' (default) or 'replace'"),
    confirm_replace: bool = Query(False, description="Must be true if mode is 'replace'")
):
    document: Optional[CaseDocument] = None

    if isinstance(payload.get("case_data"), dict):
        try:
            envelope = CaseEnvelope.model_validate(payload)
            document = (
                envelope.case_data,
                envelope.dataset_id or "DS-DEFAULT",
                envelope.dataset_version or "1.0",
            )
        except ValidationError:
            pass

    if document is None and "case_metadata" in payload:
        try:
            document = (CaseData.model_validate(payload), "DS-DEFAULT", "1.0")
        except ValidationError:
            try:
                mapped = map_ingestion_to_graph_data(payload)
                document = (mapped, "DS-DEFAULT", "1.0")
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Payload declares 'case_metadata' but could not be parsed: {str(exc)}",
                )

    if document is None:
        try:
            mapped = map_ingestion_to_graph_data(payload)
            document = (mapped, "DS-DEFAULT", "1.0")
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid payload structure: could not parse into CaseData: {str(exc)}",
            )

    case_data, dataset_id, dataset_version = document
    if not case_data.case_metadata or not case_data.case_metadata.case_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required 'case_metadata.case_id' in ingestion payload.",
        )

    try:
        with db.get_session() as session:
            ingest_result = IngestionService.ingest_case(
                session=session,
                case_data=case_data,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                mode=mode,
                confirm_replace=confirm_replace
            )
            if ingest_result.created.nodes > 0 and ingest_result.matched_existing_entities == 0:
                response.status_code = status.HTTP_201_CREATED
            else:
                response.status_code = status.HTTP_200_OK
            return ingest_result
    except Exception as e:
        logger.exception(f"Error saving case JSON ingestion to Neo4j: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to ingest case JSON into Neo4j: {str(e)}"
        )


@router.post("/ingest/text", response_model=IngestResponse, summary="Ingest raw text payload directly to Neo4j")
async def ingest_text(
    response: Response,
    payload: str = Body(..., media_type="text/plain", description="Raw case file narrative, CSV data, or FIR text"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None)
):
    if not payload or not payload.strip():
        raise HTTPException(status_code=400, detail="Payload body cannot be empty.")

    active_key = x_api_key or api_key
    engine = CaseIngestionEngine(api_key=active_key)
    engine.parse_file("input_payload.txt", payload)

    consolidated_case = engine.consolidate()
    graph_case_data = map_ingestion_to_graph_data(consolidated_case.model_dump())

    try:
        with db.get_session() as session:
            ingest_result = IngestionService.ingest_case(
                session=session,
                case_data=graph_case_data,
                dataset_id="DS-DEFAULT",
                dataset_version="1.0",
                mode="merge"
            )
            response.status_code = status.HTTP_201_CREATED
            return ingest_result
    except Exception as e:
        logger.exception(f"Error saving case text ingestion to Neo4j: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to ingest case data into Neo4j: {str(e)}"
        )

