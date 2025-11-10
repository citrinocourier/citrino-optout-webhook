# optout_webhook_fastapi.py
import os, re, json, time, logging
from datetime import datetime, timezone
from typing import Optional, Iterable, List

import gspread
from fastapi import FastAPI, Form
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from oauth2client.service_account import ServiceAccountCredentials
from twilio.twiml.messaging_response import MessagingResponse

# ===================== App & log =====================
app = FastAPI(title="Citrino Opt-Out Webhook")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("optout")

# ===================== Google auth =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON") or ""
if not CREDS_JSON:
    raise RuntimeError("Falta GOOGLE_CREDS_JSON")

try:
    CREDS = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(CREDS_JSON), SCOPES)
    GCLIENT = gspread.authorize(CREDS)
except Exception as e:
    raise RuntimeError(f"Google auth failed: {e}")

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
SHEET_NAME     = os.getenv("SHEET_NAME", "").strip()          # alternativo si no usas ID
WORKSHEET_NAME = (os.getenv("WORKSHEET_NAME", "OPT-OUT LOGS") or "OPT-OUT LOGS").strip()

EXPECTED_HEADERS: List[str] = [
    "timestamp","from","keyword","channel","status","action","reason","to"
]

def normalize_sheet_id(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise RuntimeError("Falta SPREADSHEET_ID")
    if "/d/" in raw:
        raw = raw.split("/d/")[1].split("/")[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,}", raw):
        raise RuntimeError("SPREADSHEET_ID inválido (revisa O/0, l/I)")
    return raw

def open_spreadsheet():
    if SPREADSHEET_ID:
        return GCLIENT.open_by_key(normalize_sheet_id(SPREADSHEET_ID))
    if SHEET_NAME:
        return GCLIENT.open(SHEET_NAME)
    raise RuntimeError("Configura SPREADSHEET_ID o SHEET_NAME en las variables de entorno")

def ensure_ws():
    """Obtiene o crea la hoja de trabajo y asegura los encabezados."""
    ss = open_spreadsheet()
    try:
        ws = ss.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=10)

    try:
        header = ws.get_values("1:1")
        header = header[0] if header else []
    except Exception:
        header = []

    if header[:len(EXPECTED_HEADERS)] != EXPECTED_HEADERS:
        ws.clear()
        ws.update("A1:H1", [EXPECTED_HEADERS])
        ws.freeze(rows=1)
    return ws

def backoff_delays() -> Iterable[float]:
    for d in (0.3, 0.6, 1.2, 2.5):
        yield d

def sheets_append_row(values: List[str]) -> None:
    """Append con reintentos suaves para evitar 429/5xx."""
    last_err = None
    for delay in backoff_delays():
        try:
            ws = ensure_ws()
            ws.append_row(values, value_input_option="RAW", table_range="A1")
            return
        except Exception as e:
            last_err = e
            log.warning("append_row fallo: %s -> retry %.1fs", e, delay)
            time.sleep(delay)
    # intento final
    ws = ensure_ws()
    ws.append_row(values, value_input_option="RAW", table_range="A1")

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def should_log_message(body: Optional[str]) -> bool:
    if not body:
        return False
    b = body.strip().lower()
    if b in {"stop", "start", "help"}:
        return True
    # permitir texto breve humano y filtrar trazas típicas
    if len(b) <= 160 and not any(x in b for x in (
        "http error", "twilio returned", "unable to create record", "invalid", "error:",
        "21610", "21211", "21212"
    )):
        return True
    return False

# ===================== Health & root =====================
@app.get("/healthz", response_class=JSONResponse)
def healthz():
    try:
        ss = open_spreadsheet()
        ensure_ws()  # valida acceso a la pestaña
        return {"ok": True, "sheet": ss.title, "tab": WORKSHEET_NAME}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.get("/", response_class=JSONResponse)
def root():
    return {"ok": True, "service": "citrino-optout-webhook"}

@app.head("/", include_in_schema=False)
def root_head():
    # Para que los HEAD checks de Render no devuelvan 404
    return Response(status_code=200)

# ===================== Twilio inbound =====================
@app.post("/sms/optout")
async def sms_optout(
    From: str = Form(...),
    To:   str = Form(""),
    Body: str = Form("")
):
    """
    Twilio envía application/x-www-form-urlencoded con From, To, Body
    """
    msg = MessagingResponse()

    if not should_log_message(Body):
        # Responder vacío pero válido para Twilio si es ruido técnico
        return PlainTextResponse("<Response></Response>", media_type="application/xml")

    b = (Body or "").strip().lower()
    if b == "stop":
        action, status, reason, reply = (
            "Opt-out", "Received", "user_sent_stop",
            "Has sido dado de baja de los mensajes de Citrino Courier. 🟢 Gracias."
        )
    elif b == "start":
        action, status, reason, reply = (
            "Opt-in", "Received", "user_sent_start",
            "Has sido dado de alta nuevamente. ✅"
        )
    elif b == "help":
        action, status, reason, reply = (
            "Help", "Received", "user_asked_help",
            "Ayuda: Responde STOP para salir, START para volver a entrar."
        )
    else:
        action, status, reason, reply = ("Message", "Received", "free_text", "Recibido. Gracias.")

    # Log → Google Sheets con 8 columnas esperadas
    try:
        sheets_append_row([
            now_str(),
            (From or "").strip(),
            (Body or "").strip(),
            "SMS",
            status,
            action,
            reason,
            (To or "").strip(),
        ])
    except Exception as e:
        log.error("[Sheets error] %s", e)

    msg.message(reply)
    return PlainTextResponse(str(msg), media_type="application/xml")
