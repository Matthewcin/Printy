import os
import threading
import base64
import mimetypes
import uuid
import time
import requests
import fitz

from telebot.apihelper import ApiTelegramException
from io import BytesIO

import telebot
from telebot import types
from PIL import Image
from flask import Flask
import psycopg2


# ==============================
# CONFIGURACION
# ==============================

TOKEN = os.environ["BOT_TOKEN"]
PASSWORD = os.environ["BOT_PASSWORD"]
DATABASE_URL = os.environ["DATABASE_URL"]

RESEND_API_KEY = os.environ["RESEND_API_KEY"]
EMAIL_SENDER = os.environ["EMAIL_FROM"]

PRINTER_EMAIL = "yellower@print.brother.com"

MAX_FILES = 10
MAX_TOTAL_SIZE = 20 * 1024 * 1024  # 20 MB

ALLOWED_EXTENSIONS = {
    ".pdf",
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff",
    ".txt",
    ".doc", ".docx",
    ".xls", ".xlsx",
    ".ppt", ".pptx"
}

CONVERTIBLE_IMAGES = {
    ".jpg", ".jpeg", ".png", ".bmp"
}


# ==============================
# BLOQUEO DE INSTANCIA
# NO MODIFICADO
# ==============================

LOCK_ID = 79404057696
LOCK_RENEW_INTERVAL = 45
LOCK_DURATION_SECONDS = 180

INSTANCE_ID = str(uuid.uuid4())
lock_stop_event = threading.Event()
lock_thread = None


# ==============================
# CONVERSOR B&N PDF
# ==============================

def convert_pdf_to_bw(file_bytes, dpi=200):

    source_pdf = fitz.open(
        stream=file_bytes,
        filetype="pdf"
    )

    output_pdf = fitz.open()

    try:

        for page in source_pdf:

            pix = page.get_pixmap(
                dpi=dpi,
                colorspace=fitz.csGRAY,
                alpha=False
            )

            image_bytes = pix.tobytes("png")

            new_page = output_pdf.new_page(
                width=page.rect.width,
                height=page.rect.height
            )

            new_page.insert_image(
                new_page.rect,
                stream=image_bytes
            )

        output = BytesIO()

        output_pdf.save(
            output,
            garbage=4,
            deflate=True
        )

        return output.getvalue()

    finally:

        source_pdf.close()
        output_pdf.close()


# ==============================
# GENERAR PREVIEW PDF
# ==============================

def generate_pdf_preview(file_bytes, mode="color", dpi=120):

    pdf_bytes = file_bytes

    # En blanco y negro, usar exactamente
    # el mismo PDF convertido que se enviara a imprimir.

    if mode == "bw":

        pdf_bytes = convert_pdf_to_bw(
            file_bytes
        )

    pdf = fitz.open(
        stream=pdf_bytes,
        filetype="pdf"
    )

    try:

        if pdf.page_count == 0:
            return None

        # Obtener solamente la primera pagina.

        page = pdf[0]

        pix = page.get_pixmap(
            dpi=dpi,
            alpha=False
        )

        image_bytes = pix.tobytes("png")

        output = BytesIO(image_bytes)
        output.seek(0)

        return output

    finally:

        pdf.close()


# ==============================
# BASE DE DATOS
# ==============================

def get_db_connection():

    return psycopg2.connect(DATABASE_URL)


def init_db():

    conn = get_db_connection()

    try:

        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS authorized_users (
                chat_id BIGINT PRIMARY KEY
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_instance_lock (
                lock_id BIGINT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL
            )
        """)

        conn.commit()
        cur.close()

    finally:

        conn.close()


def get_auth_users():

    conn = get_db_connection()

    try:

        cur = conn.cursor()

        cur.execute(
            "SELECT chat_id FROM authorized_users"
        )

        rows = cur.fetchall()

        cur.close()

        return set(row[0] for row in rows)

    finally:

        conn.close()


def add_auth_user(chat_id):

    conn = get_db_connection()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO authorized_users (chat_id)
            VALUES (%s)
            ON CONFLICT (chat_id) DO NOTHING
            """,
            (chat_id,)
        )

        conn.commit()
        cur.close()

    finally:

        conn.close()


# ==============================
# BLOQUEO DE INSTANCIA
# NO MODIFICADO
# ==============================

def acquire_bot_lock():

    conn = get_db_connection()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO bot_instance_lock (
                lock_id,
                owner_id,
                expires_at
            )
            VALUES (
                %s,
                %s,
                NOW() + (%s * INTERVAL '1 second')
            )
            ON CONFLICT (lock_id)
            DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                expires_at = EXCLUDED.expires_at
            WHERE bot_instance_lock.expires_at < NOW()
            RETURNING owner_id
            """,
            (
                LOCK_ID,
                INSTANCE_ID,
                LOCK_DURATION_SECONDS
            )
        )

        result = cur.fetchone()

        conn.commit()
        cur.close()

        if result is None:

            raise RuntimeError(
                "Otra instancia del bot ya está ejecutándose. "
                "No se iniciará Telegram."
            )

        print("Bloqueo de instancia adquirido correctamente.")

    finally:

        conn.close()


def renew_bot_lock():

    conn = get_db_connection()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            UPDATE bot_instance_lock
            SET expires_at = NOW() + (%s * INTERVAL '1 second')
            WHERE lock_id = %s
            AND owner_id = %s
            RETURNING owner_id
            """,
            (
                LOCK_DURATION_SECONDS,
                LOCK_ID,
                INSTANCE_ID
            )
        )

        result = cur.fetchone()

        conn.commit()
        cur.close()

        return result is not None

    finally:

        conn.close()


def release_bot_lock():

    conn = get_db_connection()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            DELETE FROM bot_instance_lock
            WHERE lock_id = %s
            AND owner_id = %s
            """,
            (LOCK_ID, INSTANCE_ID)
        )

        conn.commit()
        cur.close()

        print("Bloqueo de instancia liberado.")

    finally:

        conn.close()


# ==============================
# RESEND
# ==============================

def send_email_resend(
    to,
    subject,
    text=None,
    attachments=None,
    idempotency_key=None,
    html=None
):

    params = {
        "from": EMAIL_SENDER,
        "to": [to],
        "subject": subject
    }

    if text is not None:
        params["text"] = text

    if html is not None:
        params["html"] = html

    if attachments:

        params["attachments"] = []

        for filename, file_bytes, mime_type in attachments:

            params["attachments"].append({
                "filename": filename,
                "content": base64.b64encode(
                    file_bytes
                ).decode("utf-8"),
                "content_type": mime_type
            })

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    if idempotency_key:

        headers["Idempotency-Key"] = idempotency_key

    try:

        response = requests.post(
            "https://api.resend.com/emails",
            headers=headers,
            json=params,
            timeout=60
        )

        if not response.ok:

            raise Exception(
                f"Resend HTTP {response.status_code}: "
                f"{response.text}"
            )

        result = response.json()

        email_id = result.get("id")

        if not email_id:

            raise Exception(
                f"Resend no devolvio un ID: {result}"
            )

        return email_id

    except Exception as e:

        raise Exception(
            f"Error de Resend ({type(e).__name__}): {str(e)}"
        )


# ==============================
# FLASK
# ==============================

app = Flask(__name__)


@app.route("/")
def home():

    return "Bot de impresion activo"


def run_server():

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )


# ==============================
# TELEGRAM
# ==============================

bot = telebot.TeleBot(TOKEN)

auth_users = set()
user_data = {}

callback_lock = threading.Lock()


# ==============================
# RENOVACION DEL BLOQUEO
# NO MODIFICADO
# ==============================

def lock_heartbeat():

    while not lock_stop_event.wait(LOCK_RENEW_INTERVAL):

        try:

            if not renew_bot_lock():

                print(
                    "ERROR: Se perdió el bloqueo de instancia. "
                    "Deteniendo Telegram."
                )

                bot.stop_polling()
                return

            print("Bloqueo de instancia renovado.")

        except Exception as e:

            print(
                "ERROR renovando el bloqueo: "
                f"{type(e).__name__}: {e}"
            )

            bot.stop_polling()
            return


# ==============================
# TECLADOS INLINE
# ==============================

def main_menu_keyboard():

    markup = types.InlineKeyboardMarkup()

    markup.row(
        types.InlineKeyboardButton(
            "🖨️ IMPRIMIR",
            callback_data="main_print"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "📖 GUIA",
            callback_data="main_guide"
        )
    )

    return markup


def cancel_keyboard():

    markup = types.InlineKeyboardMarkup()

    markup.add(
        types.InlineKeyboardButton(
            "❌ CANCELAR",
            callback_data="cancel"
        )
    )

    return markup


def upload_keyboard():

    markup = types.InlineKeyboardMarkup()

    markup.row(
        types.InlineKeyboardButton(
            "🎨 ELIGE EL COLOR",
            callback_data="choose_mode"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "❌ CANCELAR",
            callback_data="cancel"
        )
    )

    return markup


def mode_keyboard():

    markup = types.InlineKeyboardMarkup()

    markup.row(
        types.InlineKeyboardButton(
            "🌈 A COLOR",
            callback_data="mode_color"
        ),
        types.InlineKeyboardButton(
            "⚫ BLANCO Y NEGRO",
            callback_data="mode_bw"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "⬅️ VOLVER",
            callback_data="back_upload"
        ),
        types.InlineKeyboardButton(
            "❌ CANCELAR",
            callback_data="cancel"
        )
    )

    return markup


def confirm_keyboard():

    markup = types.InlineKeyboardMarkup()

    # NUEVO: BOTON DE PREVIEW

    markup.row(
        types.InlineKeyboardButton(
            "👁 VER PREVIEW",
            callback_data="preview"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "✅ CONFIRMAR IMPRESION",
            callback_data="confirm_print"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "⬅️ VOLVER",
            callback_data="back_mode"
        ),
        types.InlineKeyboardButton(
            "❌ CANCELAR",
            callback_data="cancel"
        )
    )

    return markup


def back_main_keyboard():

    markup = types.InlineKeyboardMarkup()

    markup.add(
        types.InlineKeyboardButton(
            "⬅️ VOLVER AL MENU",
            callback_data="main_menu"
        )
    )

    return markup


# ==============================
# MENU PRINCIPAL
# ==============================

def show_main_menu(chat_id):

    bot.send_message(
        chat_id,
        "🏠 MENU PRINCIPAL\n\n"
        "¿Que deseas hacer?",
        reply_markup=main_menu_keyboard()
    )


# ==============================
# INICIAR IMPRESION
# ==============================

def start_printing(chat_id):

    user_data[chat_id] = {
        "files": [],
        "total_size": 0,
        "collecting": True,
        "processing": False,
        "mode": None,
        "batch_id": str(uuid.uuid4())
    }

    bot.send_message(
        chat_id,
        "📥 ENVIAR ARCHIVOS\n\n"
        "Puedes enviar hasta 10 archivos "
        "con un maximo de 20 MB en total.\n\n"
        "Envia tus documentos o fotografias "
        "uno por uno.\n\n"
        "Cuando termines, pulsa ELEGIR COLOR.",
        reply_markup=cancel_keyboard()
    )


# ==============================
# GUIA
# ==============================

def show_guide(chat_id):

    guide = (
        "📖 GUIA DE IMPRESION\n\n"

        "📁 ARCHIVOS ADMITIDOS\n\n"

        "DOCUMENTOS:\n"
        "• PDF\n"
        "• TXT\n"
        "• DOC / DOCX\n"
        "• XLS / XLSX\n"
        "• PPT / PPTX\n\n"

        "IMAGENES:\n"
        "• JPG / JPEG\n"
        "• PNG\n"
        "• BMP\n"
        "• GIF\n"
        "• TIF / TIFF\n\n"

        "📦 LIMITES POR ENVIO\n\n"
        "• Maximo 10 archivos por tanda.\n"
        "• Maximo 20 MB en total por correo.\n\n"

        "🖨️ MODOS DE IMPRESION\n\n"

        "🌈 A COLOR:\n"
        "Los archivos se envian sin convertir.\n\n"

        "⚫ BLANCO Y NEGRO:\n"
        "Los PDF se convierten a escala de grises.\n"
        "Las imagenes compatibles se convierten "
        "a escala de grises usando Pillow.\n\n"

        "👁 PREVIEW:\n"
        "Puedes visualizar la primera pagina de cada PDF "
        "antes de confirmar la impresion.\n\n"

        "📌 COMO IMPRIMIR\n\n"

        "1. Pulsa IMPRIMIR.\n"
        "2. Envia tus archivos uno por uno.\n"
        "3. Pulsa ELEGIR COLOR.\n"
        "4. Selecciona A COLOR o BLANCO Y NEGRO.\n"
        "5. Opcionalmente, pulsa VER PREVIEW.\n"
        "6. Confirma la impresion.\n\n"

        "⚠️ INFORMACION IMPORTANTE\n\n"

        "• JPG, JPEG, PNG y BMP se pueden convertir.\n"
        "• GIF y TIFF se envian en su formato original.\n"
        "• Los documentos de Office se envian sin modificar.\n"
        "• El tamaño de papel y otras opciones dependen "
        "de la configuracion de la Brother.\n"
        "• La aceptacion del correo no garantiza que "
        "la impresora haya terminado de imprimir."
    )

    bot.send_message(
        chat_id,
        guide,
        reply_markup=back_main_keyboard()
    )


# ==============================
# AUTENTICACION
# ==============================

@bot.message_handler(commands=["start"])
def send_welcome(message):

    chat_id = message.chat.id

    if chat_id in auth_users:

        show_main_menu(chat_id)

    else:

        bot.send_message(
            chat_id,
            "Ingrese la contraseña:"
        )


@bot.message_handler(
    func=lambda message: message.chat.id not in auth_users
)
def check_auth(message):

    chat_id = message.chat.id

    if (
        message.content_type == "text"
        and message.text == PASSWORD
    ):

        auth_users.add(chat_id)

        add_auth_user(chat_id)

        bot.send_message(
            chat_id,
            "Acceso concedido."
        )

        show_main_menu(chat_id)

    else:

        bot.send_message(
            chat_id,
            "Contraseña incorrecta. Ingrese la contraseña:"
        )


# ==============================
# ENVIAR PREVIEWS
# ==============================

def send_previews(chat_id):

    data = user_data.get(chat_id)

    if not data:

        bot.send_message(
            chat_id,
            "No hay una tanda activa."
        )

        return

    mode = data.get("mode", "color")

    pdf_files = [
        file for file in data["files"]
        if file["extension"] == ".pdf"
    ]

    if not pdf_files:

        bot.send_message(
            chat_id,
            "📄 No hay archivos PDF para mostrar."
        )

        return

    bot.send_message(
        chat_id,
        f"👁 GENERANDO PREVIEWS\n\n"
        f"Se mostrara la primera pagina de "
        f"{len(pdf_files)} PDF."
    )

    for index, file in enumerate(pdf_files, start=1):

        try:

            preview = generate_pdf_preview(
                file["bytes"],
                mode=mode,
                dpi=120
            )

            if preview is None:

                bot.send_message(
                    chat_id,
                    f"⚠️ {file['filename']}: el PDF esta vacio."
                )

                continue

            bot.send_photo(
                chat_id,
                preview,
                caption=(
                    f"📄 PREVIEW {index}/{len(pdf_files)}\n"
                    f"{file['filename']}\n"
                    "Primera pagina"
                )
            )

        except Exception as e:

            print(
                f"Error generando preview de "
                f"{file['filename']}: {type(e).__name__}: {e}"
            )

            bot.send_message(
                chat_id,
                f"❌ No se pudo generar la preview de "
                f"{file['filename']}.\n"
                f"Detalle: {str(e)}"
            )

    bot.send_message(
        chat_id,
        "✅ PREVIEWS FINALIZADAS\n\n"
        "Cuando estes conforme, puedes volver al resumen "
        "y confirmar la impresion.",
        reply_markup=confirm_keyboard()
    )


# ==============================
# CALLBACKS DE TELEGRAM
# ==============================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):

    chat_id = call.message.chat.id

    if chat_id not in auth_users:

        bot.answer_callback_query(
            call.id,
            "No tienes autorización."
        )

        return

    action = call.data

    bot.answer_callback_query(call.id)

    # --------------------------
    # MENU PRINCIPAL
    # --------------------------

    if action == "main_menu":

        user_data.pop(chat_id, None)

        show_main_menu(chat_id)

    elif action == "main_print":

        start_printing(chat_id)

    elif action == "main_guide":

        show_guide(chat_id)

    # --------------------------
    # CANCELAR
    # --------------------------

    elif action == "cancel":

        user_data.pop(chat_id, None)

        bot.send_message(
            chat_id,
            "❌ IMPRESION CANCELADA\n\n"
            "Se descartaron los archivos pendientes.",
            reply_markup=main_menu_keyboard()
        )

    # --------------------------
    # ELEGIR MODO
    # --------------------------

    elif action == "choose_mode":

        data = user_data.get(chat_id)

        if not data or not data.get("files"):

            bot.send_message(
                chat_id,
                "Todavia no has enviado ningun archivo.",
                reply_markup=cancel_keyboard()
            )

            return

        if not data.get("collecting"):

            bot.send_message(
                chat_id,
                "Esta tanda ya no acepta archivos."
            )

            return

        bot.send_message(
            chat_id,
            "🖨️ SELECCIONA EL MODO DE IMPRESION\n\n"
            "🌈 A COLOR: conserva los archivos originales.\n\n"
            "⚫ BLANCO Y NEGRO: convierte los PDF "
            "y las imagenes compatibles a escala de grises.",
            reply_markup=mode_keyboard()
        )

    # --------------------------
    # VOLVER A CARGAR ARCHIVOS
    # --------------------------

    elif action == "back_upload":

        data = user_data.get(chat_id)

        if not data:

            show_main_menu(chat_id)
            return

        data["mode"] = None
        data["collecting"] = True

        bot.send_message(
            chat_id,
            "📥 Puedes seguir enviando archivos.\n\n"
            "Cuando termines, pulsa ELEGIR COLOR.",
            reply_markup=upload_keyboard()
        )

    # --------------------------
    # MODO COLOR
    # --------------------------

    elif action == "mode_color":

        data = user_data.get(chat_id)

        if not data or not data.get("files"):

            bot.send_message(
                chat_id,
                "No hay archivos en esta tanda."
            )

            return

        data["mode"] = "color"
        data["collecting"] = False

        show_confirmation(chat_id, data)

    # --------------------------
    # MODO BLANCO Y NEGRO
    # --------------------------

    elif action == "mode_bw":

        data = user_data.get(chat_id)

        if not data or not data.get("files"):

            bot.send_message(
                chat_id,
                "No hay archivos en esta tanda."
            )

            return

        data["mode"] = "bw"
        data["collecting"] = False

        show_confirmation(chat_id, data)

    # --------------------------
    # VOLVER A ELEGIR MODO
    # --------------------------

    elif action == "back_mode":

        data = user_data.get(chat_id)

        if not data:

            show_main_menu(chat_id)
            return

        bot.send_message(
            chat_id,
            "Selecciona nuevamente el modo de impresion:",
            reply_markup=mode_keyboard()
        )

    # --------------------------
    # NUEVO: VER PREVIEW
    # --------------------------

    elif action == "preview":

        data = user_data.get(chat_id)

        if not data or not data.get("files"):

            bot.send_message(
                chat_id,
                "No hay archivos para mostrar."
            )

            return

        if data.get("collecting"):

            bot.send_message(
                chat_id,
                "Primero debes seleccionar el modo de impresion."
            )

            return

        if data.get("processing"):

            bot.send_message(
                chat_id,
                "La tanda ya se esta procesando."
            )

            return

        send_previews(chat_id)

    # --------------------------
    # CONFIRMAR IMPRESION
    # --------------------------

    elif action == "confirm_print":

        with callback_lock:

            data = user_data.get(chat_id)

            if not data or not data.get("files"):

                bot.send_message(
                    chat_id,
                    "No hay archivos para imprimir."
                )

                return

            if data.get("processing"):

                bot.send_message(
                    chat_id,
                    "Esta tanda ya se esta procesando."
                )

                return

            if data.get("collecting"):

                bot.send_message(
                    chat_id,
                    "Primero debes seleccionar el modo."
                )

                return

            if data.get("mode") not in ("color", "bw"):

                bot.send_message(
                    chat_id,
                    "Selecciona un modo de impresion."
                )

                return

            data["processing"] = True

            files = data["files"]
            batch_id = data["batch_id"]
            mode = data["mode"]

            user_data.pop(chat_id, None)

        send_to_printer(
            chat_id,
            files,
            batch_id,
            mode
        )

        show_main_menu(chat_id)


# ==============================
# CONFIRMACION
# ==============================

def show_confirmation(chat_id, data):

    mode = data["mode"]

    if mode == "color":

        mode_text = "🌈 A COLOR"

    else:

        mode_text = "⚫ BLANCO Y NEGRO"

    total_mb = data["total_size"] / (1024 * 1024)

    pdf_count = sum(
        1 for file in data["files"]
        if file["extension"] == ".pdf"
    )

    preview_text = ""

    if pdf_count > 0:

        preview_text = (
            f"\nPDF para previsualizar: {pdf_count}"
        )

    bot.send_message(
        chat_id,
        "📋 RESUMEN DE IMPRESION\n\n"
        f"Archivos: {len(data['files'])}/{MAX_FILES}\n"
        f"Tamaño total: {total_mb:.2f} MB\n"
        f"Modo: {mode_text}"
        f"{preview_text}\n\n"
        "Puedes ver la primera pagina de cada PDF "
        "antes de imprimir.\n\n"
        "¿Deseas enviar estos archivos a la impresora?",
        reply_markup=confirm_keyboard()
    )


# ==============================
# AGREGAR ARCHIVOS A LA TANDA
# ==============================

def add_file_to_batch(chat_id, filename, file_bytes):

    data = user_data.get(chat_id)

    if not data or not data.get("collecting"):

        bot.send_message(
            chat_id,
            "Primero pulsa IMPRIMIR para iniciar una tanda."
        )

        return

    extension = os.path.splitext(filename)[1].lower()

    if extension not in ALLOWED_EXTENSIONS:

        bot.send_message(
            chat_id,
            f"Formato no admitido: {extension or 'desconocido'}"
        )

        return

    if len(data["files"]) >= MAX_FILES:

        bot.send_message(
            chat_id,
            "Has alcanzado el limite de 10 archivos."
        )

        return

    file_size = len(file_bytes)

    if data["total_size"] + file_size > MAX_TOTAL_SIZE:

        bot.send_message(
            chat_id,
            "Este archivo supera el espacio disponible.\n"
            "El maximo total es de 20 MB."
        )

        return

    data["files"].append({
        "filename": filename,
        "bytes": file_bytes,
        "extension": extension
    })

    data["total_size"] += file_size

    total_mb = data["total_size"] / (1024 * 1024)

    bot.send_message(
        chat_id,
        "✅ ARCHIVO RECIBIDO\n\n"
        f"Nombre: {filename}\n"
        f"Archivos: {len(data['files'])}/{MAX_FILES}\n"
        f"Tamano total: {total_mb:.2f} MB / 20 MB",
        reply_markup=upload_keyboard()
    )

    if len(data["files"]) >= MAX_FILES:

        bot.send_message(
            chat_id,
            "Has alcanzado los 10 archivos. "
            "Ya puedes elegir el modo de impresion.",
            reply_markup=upload_keyboard()
        )


# ==============================
# RECIBIR FOTOGRAFIAS
# ==============================

@bot.message_handler(content_types=["photo"])
def handle_photo(message):

    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    data = user_data.get(chat_id)

    if not data or not data.get("collecting"):

        bot.send_message(
            chat_id,
            "Primero pulsa IMPRIMIR para iniciar una tanda."
        )

        return

    try:

        file_info = bot.get_file(
            message.photo[-1].file_id
        )

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        number = len(data.get("files", [])) + 1

        add_file_to_batch(
            chat_id,
            f"fotografia_{number}.jpg",
            downloaded_file
        )

    except Exception as e:

        bot.send_message(
            chat_id,
            f"Error al recibir la fotografia: {str(e)}"
        )


# ==============================
# RECIBIR DOCUMENTOS
# ==============================

@bot.message_handler(content_types=["document"])
def handle_document(message):

    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    data = user_data.get(chat_id)

    if not data or not data.get("collecting"):

        bot.send_message(
            chat_id,
            "Primero pulsa IMPRIMIR para iniciar una tanda."
        )

        return

    try:

        document = message.document

        filename = document.file_name or "documento"

        extension = os.path.splitext(filename)[1].lower()

        if extension not in ALLOWED_EXTENSIONS:

            bot.send_message(
                chat_id,
                "Formato no admitido.\n"
                "Consulta GUIA para ver los formatos disponibles."
            )

            return

        if (
            document.file_size
            and document.file_size > MAX_TOTAL_SIZE
        ):

            bot.send_message(
                chat_id,
                "El archivo supera el limite de 20 MB."
            )

            return

        file_info = bot.get_file(
            document.file_id
        )

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        add_file_to_batch(
            chat_id,
            filename,
            downloaded_file
        )

    except Exception as e:

        bot.send_message(
            chat_id,
            f"Error al recibir el documento: {str(e)}"
        )


# ==============================
# PREPARAR ARCHIVOS
# ==============================

def prepare_attachments(files, mode):

    attachments = []

    for file in files:

        filename = file["filename"]
        file_bytes = file["bytes"]
        extension = file["extension"]

        # PDF A BLANCO Y NEGRO

        if mode == "bw" and extension == ".pdf":

            file_bytes = convert_pdf_to_bw(
                file_bytes
            )

        # IMAGENES A BLANCO Y NEGRO

        if mode == "bw" and extension in CONVERTIBLE_IMAGES:

            with Image.open(BytesIO(file_bytes)) as original:

                img = original.convert("L")

                output = BytesIO()

                img.save(
                    output,
                    format="JPEG",
                    quality=95
                )

                filename = (
                    os.path.splitext(filename)[0] + ".jpg"
                )

                file_bytes = output.getvalue()

        mime_type, _ = mimetypes.guess_type(filename)

        if not mime_type:

            mime_type = "application/octet-stream"

        attachments.append((
            filename,
            file_bytes,
            mime_type
        ))

    return attachments


# ==============================
# ENVIO A IMPRESORA BROTHER
# ==============================

def send_to_printer(chat_id, files, batch_id, mode):

    try:

        if mode == "color":

            mode_text = "A COLOR"

        else:

            mode_text = "BLANCO Y NEGRO"

        bot.send_message(
            chat_id,
            "⏳ PREPARANDO IMPRESION\n\n"
            f"Modo seleccionado: {mode_text}\n"
            "Preparando los archivos para la impresora..."
        )

        attachments = prepare_attachments(
            files,
            mode
        )

        email_id = send_email_resend(
            to=PRINTER_EMAIL,

            subject=" ",

            html="<!-- -->",

            attachments=attachments,

            idempotency_key=f"telegram-print-{batch_id}"
        )

        bot.send_message(
            chat_id,
            "✅ ENVIANDO A LA IMPRESORA\n\n"
            f"Modo: {mode_text}\n"
            f"Archivos enviados: {len(attachments)}\n"
            "La impresion todavia debe ser confirmada "
            "por la impresora."
        )

    except Exception as e:

        bot.send_message(
            chat_id,
            "❌ ERROR AL ENVIAR A LA IMPRESORA\n\n"
            f"Tipo: {type(e).__name__}\n"
            f"Detalle: {str(e)}"
        )


# ==============================
# CORREO DE PRUEBA DESACTIVADO
# ==============================

# El correo de prueba permanece desactivado.


# ==============================
# INICIO
# SE MANTIENE COMO EN TU CODIGO
# ==============================

if __name__ == "__main__":

    init_db()

    auth_users = get_auth_users()

    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )

    server_thread.start()

    print("Bot iniciado correctamente. Conectando a Telegram...")

    try:

        while True:

            try:

                bot.infinity_polling(
                    skip_pending=True,
                    timeout=30,
                    long_polling_timeout=30
                )

                break

            except ApiTelegramException as e:

                if e.error_code == 409:

                    print(
                        "Conflicto 409: Instancia vieja aún activa "
                        "en Render. Reintentando en 10 segundos..."
                    )

                    time.sleep(10)

                else:

                    print(
                        f"Error de Telegram: {e}. Reintentando..."
                    )

                    time.sleep(5)

            except Exception as e:

                print(
                    f"Error inesperado: {e}. Reintentando en 5 segundos..."
                )

                time.sleep(5)

    except KeyboardInterrupt:

        print("Cierre solicitado.")

    finally:

        print("Apagando bot...")
