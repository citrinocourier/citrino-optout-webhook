# optout_webhook_fastapi.py
import os, re, json, time, logging
import gspread
from fastapi import FastAPI, Form
from fastapi.responses import Response
from datetime import datetime
from twilio.twiml.messaging_response import MessagingResponse
from fastapi import Response
from fastapi.responses import JSONResponse

@app.get("/healthz", response_class=JSONResponse)
def healthz():
    return {"ok": True, "service": "citrino-optout-webhook"}

@app.get("/", response_class=JSONResponse)
def root():
    return {"ok": True}

@app.head("/", include_in_schema=False)
def root_head():
    # para que los HEAD checks de Render no devuelvan 404
    return Response(status_code=200)

app = FastAPI()
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("optout")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
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

# Credenciales
creds_json = os.getenv("GOOGLE_CREDS_JSON") or ""
if not creds_json:
    raise RuntimeError("Falta GOOGLE_CREDS_JSON")
gclient = gspread.service_account_from_dict(json.loads(creds_json), scopes=SCOPES)

_ws_cache = None
def get_ws():
    global _ws_cache
    if _ws_cache is not None:
        return _ws_cache
    sid = normalize_sheet_id(os.getenv("SPREADSHEET_ID"))
    sname = os.getenv("SHEET_NAME", "Hoja 1")
    log.info("Inicializando Sheets: %s...%s / tab='%s'", sid[:6], sid[-6:], sname)
    for attempt in range(4):
        try:
            ss = gclient.open_by_key(sid)
            try:
                ws = ss.worksheet(sname)
            except gspread.WorksheetNotFound:
                ws = ss.get_worksheet(0)
            _ws_cache = ws
            return ws
        except Exception as e:
            wait = 2 ** attempt
            log.warning("get_ws fallo intento %s: %s -> retry %ss", attempt+1, e, wait)
            time.sleep(wait)
    raise RuntimeError("No se pudo abrir el worksheet tras reintentos")

@app.get("/health")
def health():
    try:
        ws = get_ws()
        return {"ok": True, "sheet": ws.spreadsheet.title, "tab": ws.title}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/sms/optout")
async def handle_optout(From: str = Form(...), Body: str = Form(...)):
    resp = MessagingResponse()
    try:
        ws = get_ws()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (Body or "").strip().lower()
        src  = (From or "").strip()
        # log recepción
        for _ in range(2):
            try:
                ws.append_row([now, src, Body, "SMS", "Received"])
                break
            except Exception as e:
                log.warning("append_row(Received) %s -> retry", e)
                time.sleep(1)

        keywords = {"stop","baja","unsubscribe","cancelar","salir"}
        if any(k in body for k in keywords):
            rows = ws.get_all_records()
            tail = re.sub(r"\D", "", src)[-10:]
            updated = False
            for i, row in enumerate(rows, start=2):
                digits = re.sub(r"\D", "", str(row.get("Phone","")))[-10:]
                if digits and digits == tail:
                    ws.update_acell(f"F{i}", "OPT_OUT")
                    ws.update_acell(f"L{i}", f"STOP {now}")
                    updated = True
                    log.info("OPT_OUT en fila %s", i)
                    break
            ws.append_row([now, src, Body, "SMS", "Opt-out" + ("" if updated else " (no match)")])
            resp.message("Has sido dado de baja de los mensajes de Citrino Courier. 🟢 Gracias.")
        else:
            resp.message("Mensaje recibido ✅. Si deseas dejar de recibir mensajes, responde STOP.")
        return Response(content=str(resp), media_type="application/xml")

    except Exception as e:
        log.error("optout ERROR: %s", e)
        resp.message("Error temporal procesando tu solicitud. Intenta de nuevo en unos minutos.")
        return Response(content=str(resp), media_type="application/xml", status_code=200)


