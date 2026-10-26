
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
from PIL import Image, ImageEnhance
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
MAX_TOTAL_SIZE = 20 * 1024 * 1024

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

CONTRAST_MIN = 0.50
CONTRAST_MAX = 2.50
CONTRAST_STEP = 0.25


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
# PROCESAMIENTO DE PDF
# ==============================

def process_pdf(file_bytes, mode="color", contrast=1.0, dpi=200):

    source_pdf = fitz.open(
        stream=file_bytes,
        filetype="pdf"
    )

    output_pdf = fitz.open()

    try:

        for page in source_pdf:

            colorspace = (
                fitz.csGRAY
                if mode == "bw"
                else fitz.csRGB
            )

            pix = page.get_pixmap(
                dpi=dpi,
                colorspace=colorspace,
                alpha=False
            )

            image_mode = "L" if mode == "bw" else "RGB"

            img = Image.frombytes(
                image_mode,
                [pix.width, pix.height],
                pix.samples
            )

            if contrast != 1.0:

                img = ImageEnhance.Contrast(img).enhance(
                    contrast
                )

            image_output = BytesIO()

            img.save(
                image_output,
                format="PNG"
            )

            new_page = output_pdf.new_page(
                width=page.rect.width,
                height=page.rect.height
            )

            new_page.insert_image(
                new_page.rect,
                stream=image_output.getvalue()
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


def generate_pdf_preview(file_bytes, mode="color", contrast=1.0):

    # Se procesa con los mismos ajustes que el archivo final.

    processed = process_pdf(
        file_bytes,
        mode=mode,
        contrast=contrast,
        dpi=200
    )

    pdf = fitz.open(
        stream=processed,
        filetype="pdf"
    )

    try:

        if pdf.page_count == 0:
            return None

        pix = pdf[0].get_pixmap(
            dpi=120,
            alpha=False
        )

        output = BytesIO(
            pix.tobytes("png")
        )

        output.seek(0)

        return output

    finally:

        pdf.close()


# ==============================
# PROCESAMIENTO DE IMAGENES
# ==============================

def process_image(file_bytes, mode="color", contrast=1.0):

    with Image.open(BytesIO(file_bytes)) as original:

        if mode == "bw":

            img = original.convert("L")

        else:

            img = original.convert("RGB")

        if contrast != 1.0:

            img = ImageEnhance.Contrast(img).enhance(
                contrast
            )

        output = BytesIO()

        img.save(
            output,
            format="JPEG",
            quality=95
        )

        return output.getvalue()


def generate_image_preview(file_bytes, mode="color", contrast=1.0):

    processed = process_image(
        file_bytes,
        mode=mode,
        contrast=contrast
    )

    output = BytesIO(processed)
    output.seek(0)

    return output


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

    response = requests.post(
        "https://api.resend.com/emails",
        headers=headers,
        json=params,
        timeout=60
    )

    if not response.ok:

        raise Exception(
            f"Resend HTTP {response.status_code}: {response.text}"
        )

    result = response.json()

    email_id = result.get("id")

    if not email_id:

        raise Exception(
            f"Resend no devolvió un ID: {result}"
        )

    return email_id


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
# UTILIDADES DE PREVIEW
# ==============================

def delete_current_preview(chat_id):

    data = user_data.get(chat_id)

    if not data:
        return

    message_id = data.get("preview_message_id")

    if not message_id:
        return

    try:

        bot.delete_message(
            chat_id,
            message_id
        )

    except Exception as e:

        print(
            f"No se pudo borrar la preview: {e}"
        )

    data["preview_message_id"] = None


def is_previewable(file):

    return (
        file["extension"] == ".pdf"
        or file["extension"] in CONVERTIBLE_IMAGES
    )


def send_file_preview(chat_id, index):

    data = user_data.get(chat_id)

    if not data:
        return False

    if index < 0 or index >= len(data["files"]):
        return False

    file = data["files"][index]

    if not is_previewable(file):

        bot.send_message(
            chat_id,
            "Este formato no permite generar una preview."
        )

        return False

    mode = file["mode"]
    contrast = file["contrast"]

    try:

        if file["extension"] == ".pdf":

            preview = generate_pdf_preview(
                file["bytes"],
                mode=mode,
                contrast=contrast
            )

        else:

            preview = generate_image_preview(
                file["bytes"],
                mode=mode,
                contrast=contrast
            )

        if preview is None:
            return False

        caption = (
            f"👁 PREVIEW {index + 1}/{len(data['files'])}\n"
            f"📄 {file['filename']}\n"
            f"Modo: {'A COLOR' if mode == 'color' else 'BLANCO Y NEGRO'}\n"
            f"Contraste: {contrast:.2f}x"
        )

        # Primero se envía la nueva preview.
        # Solo si funciona se elimina la anterior.

        if file["extension"] == ".pdf":

            sent = bot.send_photo(
                chat_id,
                preview,
                caption=caption
            )

        else:

            sent = bot.send_photo(
                chat_id,
                preview,
                caption=caption
            )

        old_message_id = data.get("preview_message_id")

        data["preview_message_id"] = sent.message_id
        data["preview_index"] = index

        if old_message_id:

            try:

                bot.delete_message(
                    chat_id,
                    old_message_id
                )

            except Exception as e:

                print(
                    f"No se pudo borrar la preview anterior: {e}"
                )

        return True

    except Exception as e:

        print(
            f"Error generando preview: {type(e).__name__}: {e}"
        )

        bot.send_message(
            chat_id,
            f"❌ No se pudo generar la preview.\n{str(e)}"
        )

        return False


def next_preview_index(data, current_index):

    files = data["files"]

    for offset in range(1, len(files) + 1):

        index = (current_index + offset) % len(files)

        if is_previewable(files[index]):
            return index

    return None


# ==============================
# TECLADOS
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
            "✅ FINALIZAR CARGA",
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
            "🌈 TODOS A COLOR",
            callback_data="mode_color"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "⚫ TODOS EN BLANCO Y NEGRO",
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


def file_list_keyboard(data):

    markup = types.InlineKeyboardMarkup()

    for index, file in enumerate(data["files"]):

        changed = (
            file["mode"] != data["default_mode"]
            or file["contrast"] != 1.0
        )

        marker = "✏️ " if changed else ""

        name = file["filename"]

        if len(name) > 30:
            name = name[:27] + "..."

        markup.row(
            types.InlineKeyboardButton(
                f"{marker}{index + 1}. {name}",
                callback_data=f"file:{index}"
            )
        )

    markup.row(
        types.InlineKeyboardButton(
            "🖨️ FINALIZAR Y PREPARAR IMPRESIÓN",
            callback_data="finish_print"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "❌ CANCELAR TANDA",
            callback_data="cancel"
        )
    )

    return markup


def editor_keyboard(file):

    markup = types.InlineKeyboardMarkup()

    if is_previewable(file):

        markup.row(
            types.InlineKeyboardButton(
                "👁 VER PREVIEW",
                callback_data="preview_current"
            ),
            types.InlineKeyboardButton(
                "⏭ SIGUIENTE ARCHIVO",
                callback_data="preview_next"
            )
        )

    markup.row(
        types.InlineKeyboardButton(
            "🌈 A COLOR",
            callback_data="set_color"
        ),
        types.InlineKeyboardButton(
            "⚫ BLANCO Y NEGRO",
            callback_data="set_bw"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "➖ REDUCIR CONTRASTE",
            callback_data="contrast_down"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "➕ AUMENTAR CONTRASTE",
            callback_data="contrast_up"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "↩️ RESTAURAR CONTRASTE",
            callback_data="contrast_reset"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "↩️ CANCELAR EDICIÓN",
            callback_data="cancel_edit"
        )
    )

    markup.row(
        types.InlineKeyboardButton(
            "⬅️ VOLVER A ARCHIVOS",
            callback_data="file_list"
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
# MENUS
# ==============================

def show_main_menu(chat_id):

    bot.send_message(
        chat_id,
        "🏠 MENU PRINCIPAL\n\n"
        "¿Qué deseas hacer?",
        reply_markup=main_menu_keyboard()
    )


def show_file_list(chat_id, message_id=None):

    data = user_data.get(chat_id)

    if not data:
        return

    lines = [
        "📋 ARCHIVOS DE LA TANDA",
        "",
        "Selecciona un archivo para editarlo.",
        "",
        "✏️ indica que tiene cambios.",
        ""
    ]

    for index, file in enumerate(data["files"]):

        changed = (
            file["mode"] != data["default_mode"]
            or file["contrast"] != 1.0
        )

        marker = "✏️ " if changed else ""

        mode_text = (
            "Color"
            if file["mode"] == "color"
            else "Blanco y negro"
        )

        lines.append(
            f"{marker}{index + 1}. {file['filename']} — "
            f"{mode_text}, contraste {file['contrast']:.2f}x"
        )

    text = "\n".join(lines)

    markup = file_list_keyboard(data)

    if message_id:

        try:

            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup
            )

            data["menu_message_id"] = message_id
            return

        except Exception:
            pass

    sent = bot.send_message(
        chat_id,
        text,
        reply_markup=markup
    )

    data["menu_message_id"] = sent.message_id


def show_file_editor(chat_id, index, message_id=None):

    data = user_data.get(chat_id)

    if not data:
        return

    if index < 0 or index >= len(data["files"]):
        return

    file = data["files"][index]

    data["editing_index"] = index

    data["edit_snapshot"] = {
        "mode": file["mode"],
        "contrast": file["contrast"]
    }

    mode_text = (
        "🌈 A COLOR"
        if file["mode"] == "color"
        else "⚫ BLANCO Y NEGRO"
    )

    text = (
        f"🛠 EDITANDO ARCHIVO {index + 1}/{len(data['files'])}\n\n"
        f"📄 {file['filename']}\n\n"
        f"Modo: {mode_text}\n"
        f"Contraste: {file['contrast']:.2f}x\n\n"
        "Selecciona los ajustes que deseas aplicar.\n"
        "Los cambios se utilizarán tanto en la preview "
        "como en la impresión."
    )

    markup = editor_keyboard(file)

    if message_id:

        try:

            bot.edit_message_text(
                text,
                chat_id,
                message_id,
                reply_markup=markup
            )

            data["menu_message_id"] = message_id
            return

        except Exception:
            pass

    sent = bot.send_message(
        chat_id,
        text,
        reply_markup=markup
    )

    data["menu_message_id"] = sent.message_id


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
        "default_mode": None,
        "batch_id": str(uuid.uuid4()),
        "preview_message_id": None,
        "preview_index": None,
        "menu_message_id": None,
        "editing_index": None,
        "edit_snapshot": None
    }

    bot.send_message(
        chat_id,
        "📥 ENVIAR ARCHIVOS\n\n"
        "Puedes enviar hasta 10 archivos "
        "con un máximo de 20 MB en total.\n\n"
        "Envía tus documentos o fotografías uno por uno.\n\n"
        "Cuando termines, pulsa FINALIZAR CARGA.",
        reply_markup=upload_keyboard()
    )


# ==============================
# GUIA
# ==============================

def show_guide(chat_id):

    guide = (
        "📖 GUIA DE IMPRESION\n\n"

        "📁 ARCHIVOS ADMITIDOS\n"
        "PDF, JPG, JPEG, PNG, BMP, GIF, TIF, TIFF, "
        "TXT, DOC, DOCX, XLS, XLSX, PPT y PPTX.\n\n"

        "📦 LIMITES\n"
        "• Máximo 10 archivos por tanda.\n"
        "• Máximo 20 MB en total.\n\n"

        "🖨️ EDICION INDIVIDUAL\n"
        "Puedes configurar cada archivo por separado:\n"
        "• A color o blanco y negro.\n"
        "• Aumentar o reducir contraste.\n"
        "• Restaurar el contraste normal.\n"
        "• Cancelar la edición y recuperar los ajustes anteriores.\n\n"

        "👁 PREVIEWS\n"
        "Puedes visualizar la primera página de cada PDF "
        "y las imágenes compatibles.\n"
        "Al mostrar otra preview, la anterior se elimina "
        "automáticamente cuando la nueva se envía correctamente.\n\n"

        "⚠️ IMPORTANTE\n"
        "• PDF: se rasteriza al modificar modo o contraste.\n"
        "• JPG, JPEG, PNG y BMP: se procesan como imágenes.\n"
        "• GIF, TIFF y documentos de Office se envían sin "
        "conversión de modo ni contraste.\n"
        "• Los cambios de contraste y blanco y negro "
        "no se aplican a documentos de Office.\n"
        "• La preview muestra solo la primera página del PDF."
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
# CALLBACKS
# ==============================

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):

    chat_id = call.message.chat.id
    action = call.data
    message_id = call.message.message_id

    if chat_id not in auth_users:

        bot.answer_callback_query(
            call.id,
            "No tienes autorización."
        )

        return

    bot.answer_callback_query(call.id)

    data = user_data.get(chat_id)

    # --------------------------
    # MENU PRINCIPAL
    # --------------------------

    if action == "main_menu":

        delete_current_preview(chat_id)

        user_data.pop(chat_id, None)

        show_main_menu(chat_id)

    elif action == "main_print":

        start_printing(chat_id)

    elif action == "main_guide":

        show_guide(chat_id)

    # --------------------------
    # CANCELAR TANDA
    # --------------------------

    elif action == "cancel":

        delete_current_preview(chat_id)

        user_data.pop(chat_id, None)

        bot.send_message(
            chat_id,
            "❌ TANDA CANCELADA\n\n"
            "Se descartaron los archivos pendientes.",
            reply_markup=main_menu_keyboard()
        )

    # --------------------------
    # FINALIZAR CARGA
    # --------------------------

    elif action == "choose_mode":

        if not data or not data["files"]:

            bot.send_message(
                chat_id,
                "Todavía no has enviado ningún archivo."
            )

            return

        if not data["collecting"]:
            return

        bot.send_message(
            chat_id,
            "🖨️ MODO INICIAL\n\n"
            "Selecciona el modo que se aplicará "
            "inicialmente a todos los archivos.\n\n"
            "Después podrás modificar cada archivo "
            "individualmente.",
            reply_markup=mode_keyboard()
        )

    # --------------------------
    # VOLVER A CARGA
    # --------------------------

    elif action == "back_upload":

        if not data:
            return

        delete_current_preview(chat_id)

        data["mode"] = None
        data["default_mode"] = None
        data["collecting"] = True

        for file in data["files"]:

            file["mode"] = None
            file["contrast"] = 1.0

        bot.send_message(
            chat_id,
            "📥 Puedes seguir enviando archivos.\n\n"
            "Cuando termines, pulsa FINALIZAR CARGA.",
            reply_markup=upload_keyboard()
        )

    # --------------------------
    # SELECCION DEL MODO INICIAL
    # --------------------------

    elif action in ("mode_color", "mode_bw"):

        if not data or not data["files"]:
            return

        mode = (
            "color"
            if action == "mode_color"
            else "bw"
        )

        data["mode"] = mode
        data["default_mode"] = mode
        data["collecting"] = False

        for file in data["files"]:

            file["mode"] = mode
            file["contrast"] = 1.0

        show_file_list(
            chat_id,
            message_id
        )

    # --------------------------
    # ABRIR ARCHIVO
    # --------------------------

    elif action.startswith("file:"):

        if not data or data["collecting"]:
            return

        try:

            index = int(action.split(":")[1])

        except ValueError:
            return

        show_file_editor(
            chat_id,
            index,
            message_id
        )

    # --------------------------
    # LISTA DE ARCHIVOS
    # --------------------------

    elif action == "file_list":

        if not data:
            return

        delete_current_preview(chat_id)

        show_file_list(
            chat_id,
            message_id
        )

    # --------------------------
    # MODO INDIVIDUAL
    # --------------------------

    elif action in ("set_color", "set_bw"):

        if not data:
            return

        index = data.get("editing_index")

        if index is None:
            return

        file = data["files"][index]

        file["mode"] = (
            "color"
            if action == "set_color"
            else "bw"
        )

        show_file_editor(
            chat_id,
            index,
            message_id
        )

    # --------------------------
    # CONTRASTE
    # --------------------------

    elif action in (
        "contrast_up",
        "contrast_down",
        "contrast_reset"
    ):

        if not data:
            return

        index = data.get("editing_index")

        if index is None:
            return

        file = data["files"][index]

        if action == "contrast_up":

            file["contrast"] = min(
                CONTRAST_MAX,
                round(file["contrast"] + CONTRAST_STEP, 2)
            )

        elif action == "contrast_down":

            file["contrast"] = max(
                CONTRAST_MIN,
                round(file["contrast"] - CONTRAST_STEP, 2)
            )

        elif action == "contrast_reset":

            file["contrast"] = 1.0

        show_file_editor(
            chat_id,
            index,
            message_id
        )

    # --------------------------
    # CANCELAR EDICION
    # --------------------------

    elif action == "cancel_edit":

        if not data:
            return

        index = data.get("editing_index")
        snapshot = data.get("edit_snapshot")

        if index is None or snapshot is None:
            return

        file = data["files"][index]

        file["mode"] = snapshot["mode"]
        file["contrast"] = snapshot["contrast"]

        delete_current_preview(chat_id)

        show_file_list(
            chat_id,
            message_id
        )

    # --------------------------
    # PREVIEW ACTUAL
    # --------------------------

    elif action == "preview_current":

        if not data:
            return

        index = data.get("editing_index")

        if index is not None:

            send_file_preview(
                chat_id,
                index
            )

    # --------------------------
    # SIGUIENTE PREVIEW
    # --------------------------

    elif action == "preview_next":

        if not data:
            return

        current = data.get("preview_index")

        if current is None:
            current = data.get("editing_index", 0)

        next_index = next_preview_index(
            data,
            current
        )

        if next_index is None:

            bot.send_message(
                chat_id,
                "No hay otros archivos con preview disponible."
            )

            return

        send_file_preview(
            chat_id,
            next_index
        )

    # --------------------------
    # FINALIZAR Y PREPARAR IMPRESION
    # --------------------------

    elif action == "finish_print":

        with callback_lock:

            data = user_data.get(chat_id)

            if not data or not data["files"]:
                return

            if data.get("processing"):
                return

            data["processing"] = True

            files = data["files"]
            batch_id = data["batch_id"]

            delete_current_preview(chat_id)

            user_data.pop(chat_id, None)

        send_to_printer(
            chat_id,
            files,
            batch_id
        )

        show_main_menu(chat_id)


# ==============================
# AGREGAR ARCHIVOS
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
            "Has alcanzado el límite de 10 archivos."
        )

        return

    file_size = len(file_bytes)

    if data["total_size"] + file_size > MAX_TOTAL_SIZE:

        bot.send_message(
            chat_id,
            "Este archivo supera el espacio disponible.\n"
            "El máximo total es de 20 MB."
        )

        return

    data["files"].append({
        "filename": filename,
        "bytes": file_bytes,
        "extension": extension,
        "mode": None,
        "contrast": 1.0
    })

    data["total_size"] += file_size

    total_mb = data["total_size"] / (1024 * 1024)

    bot.send_message(
        chat_id,
        "✅ ARCHIVO RECIBIDO\n\n"
        f"Nombre: {filename}\n"
        f"Archivos: {len(data['files'])}/{MAX_FILES}\n"
        f"Tamaño total: {total_mb:.2f} MB / 20 MB",
        reply_markup=upload_keyboard()
    )

    if len(data["files"]) >= MAX_FILES:

        bot.send_message(
            chat_id,
            "Has alcanzado los 10 archivos. "
            "Ya puedes finalizar la carga.",
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
            f"Error al recibir la fotografía: {str(e)}"
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
                "El archivo supera el límite de 20 MB."
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

def prepare_attachments(files):

    attachments = []

    for file in files:

        filename = file["filename"]
        file_bytes = file["bytes"]
        extension = file["extension"]

        mode = file.get("mode", "color")
        contrast = file.get("contrast", 1.0)

        # PDF

        if extension == ".pdf":

            if mode == "bw" or contrast != 1.0:

                file_bytes = process_pdf(
                    file_bytes,
                    mode=mode,
                    contrast=contrast,
                    dpi=200
                )

        # IMAGENES COMPATIBLES

        elif extension in CONVERTIBLE_IMAGES:

            if mode == "bw" or contrast != 1.0:

                file_bytes = process_image(
                    file_bytes,
                    mode=mode,
                    contrast=contrast
                )

                filename = (
                    os.path.splitext(filename)[0] + ".jpg"
                )

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

def send_to_printer(chat_id, files, batch_id):

    try:

        bot.send_message(
            chat_id,
            "⏳ PREPARANDO IMPRESIÓN\n\n"
            "Aplicando los ajustes individuales de cada archivo..."
        )

        attachments = prepare_attachments(files)

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
            f"Archivos enviados: {len(attachments)}\n"
            f"Referencia: {email_id}\n\n"
            "La impresión todavía debe ser confirmada "
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
