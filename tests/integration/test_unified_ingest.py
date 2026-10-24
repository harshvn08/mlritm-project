import pytest

SAMPLE_TEXT_INPUT = """
FIRST INFORMATION REPORT
FIR No: 0045/2026 | Police Station: Lajpat Nagar | Date: 2026-08-25
Sections of Law: Section 137 BNS.
Recorded by: Sub-Inspector Manoj Dwivedi

DETAILS OF COMPLAINANT:
Shri Ramesh Patel

DETAILS OF VICTIM:
Ms. Priya Patel, daughter of complainant, age 22. Mobile: 9999955555.

COMPLAINT NARRATIVE:
Witness saw Priya Patel forced into a black Scorpio plate DL-3C-AB-1234. 
Driver is Amit Sharma (Mobile: 9999933333). Coordinator is Rajesh Kumar (Mobile: 9999922222). 
Leader is Vikram Singh (Mobile: 9999911111).
Suspect Amit Sharma posted on Instagram (@amit_rider) showing the vehicle.

Sender_Account,Sender_Name,Receiver_Account,Receiver_Name,Amount_INR,Timestamp,Transaction_ID
ACC999991,Vikram Singh,ACC999993,Amit Sharma,500000,2026-08-25 10:00:00,TXN999001
ACC999993,Amit Sharma,ACC999992,Rajesh Kumar,480000,2026-08-25 12:00:00,TXN999003

Caller_MSISDN,Recipient_MSISDN,Timestamp,Duration_Sec,CellTower_ID
9999933333,9999922222,2026-08-25 14:05:00,120,TOWER_DEL_01
9999922222,9999911111,2026-08-25 14:15:00,240,TOWER_HR_01
"""

def test_unified_text_ingestion(client):
    # Post unstructured text to the new unified ingestion endpoint
    response = client.post(
        "/api/v1/ingest/text",
        content=SAMPLE_TEXT_INPUT,
        headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["case_id"] == "CASE_0045_2026"
    assert data["created"]["nodes"] >= 4
    assert data["created"]["relationships"] >= 4


def test_multipart_case_ingestion_preserves_title_and_processes_csv_txt(client):
    title = "Synthetic Network Case 2026"
    narrative = (
        "FIRST INFORMATION REPORT\nFIR No: SYN-2026-01 | Date: 2026-10-20\n"
        "Complainant Alice Morgan reports that suspect Bob Singh called her after the incident."
    )
    cdr = (
        "Caller_MSISDN,Recipient_MSISDN,Timestamp,Duration_Sec,CellTower_ID\n"
        "9876543210,9876543211,2026-10-20 10:00:00,60,TOWER_SYN_01\n"
    )

    response = client.post(
        "/api/v1/ingest?case_id=CASE_SYNTHETIC_2026",
        data={"case_title": title},
        files=[
            ("files", ("CDR-2026-041.csv", cdr, "text/csv")),
            ("files", ("FIR-2026-041.txt", narrative, "text/plain")),
            ("files", ("README.txt", "Uploaded evidence notes; ordinary document content only.", "text/plain")),
        ],
    )

    assert response.status_code == 201, response.text
    data = response.json()
    assert data["case_id"] == "CASE_SYNTHETIC_2026"
    assert data["converted_case_data"]["case_metadata"]["case_name"] == title
    assert len(data["converted_case_data"]["entities"]["phones"]) == 2
    assert len(data["converted_case_data"]["relationships"]["communications"]) == 1
    assert data["created"]["nodes"] > 0
    assert data["created"]["relationships"] > 0


def test_unsupported_multipart_file_returns_actionable_validation_error(client):
    response = client.post(
        "/api/v1/ingest?case_id=CASE_INVALID_UPLOAD",
        data={"case_title": "Invalid Upload Case"},
        files=[("files", ("evidence.exe", b"not an evidence file", "application/octet-stream"))],
    )

    assert response.status_code == 415
    assert "not supported" in response.json()["detail"].lower()

