from fastapi import FastAPI, Form
from fastapi.responses import Response
import gspread
from datetime import datetime
from twilio.twiml.messaging_response import MessagingResponse
import uvicorn

app = FastAPI()

# === Google Sheets Config ===
SPREADSHEET_ID = "1BZot_2EwjpGNdvypn9hyT410gKSp0neEW4ySXN8Po3E"
SHEET_NAME = "Hoja 1"

gc = gspread.service_account(filename="google-creds.json")
ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

@app.post("/sms/optout")
async def handle_optout(From: str = Form(...), Body: str = Form(...)):
    body = Body.strip().lower()
    print(f"📩 Mensaje recibido de {From}: {body}")

    # Twilio Response
    response = MessagingResponse()

    # Palabras clave de baja
    keywords = ["stop", "baja", "unsubscribe", "cancelar", "salir"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # === REGISTRO DE CADA MENSAJE ===
    ws.append_row([now, From, Body, "SMS", "Received"])

    # === MANEJO DE OPT-OUT ===
    if any(k in body for k in keywords):
        rows = ws.get_all_records()
        for i, row in enumerate(rows, start=2):
            phone = str(row.get("Phone", "")).strip()
            clean_number = "+1" + phone if not phone.startswith("+") else phone
            if clean_number.endswith(From[-10:]):
                ws.update_acell(f"F{i}", "OPT_OUT")
                ws.update_acell(f"L{i}", f"STOP {now}")
                print(f"✅ {From} marcado como OPT_OUT en fila {i}")
                break

        # Registrar también en log general
        ws.append_row([now, From, Body, "SMS", "Opt-out"])
        response.message("Has sido dado de baja de los mensajes de Citrino Courier. 🟢 Gracias.")
    else:
        response.message("Mensaje recibido ✅. Si deseas dejar de recibir mensajes, responde STOP.")

    return Response(content=str(response), media_type="application/xml")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)
