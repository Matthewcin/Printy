import os
import threading
import smtplib
from email.message import EmailMessage
from io import BytesIO

import socket
import ssl
import smtplib

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

PRINTER_EMAIL = "79404057696@print.brother.com"
TEST_EMAIL = "pendoo8x@gmail.com"

EMAIL_SENDER = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]

SMTP_SERVER = "smtp-mail.outlook.com"
SMTP_PORT = 587

DATABASE_URL = os.environ["DATABASE_URL"]


# ==============================
# BASE DE DATOS
# ==============================

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS authorized_users (
            chat_id BIGINT PRIMARY KEY
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def get_auth_users():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT chat_id FROM authorized_users")
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return set(row[0] for row in rows)


def add_auth_user(chat_id):
    conn = psycopg2.connect(DATABASE_URL)
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
    conn.close()


# ==============================
# FLASK
# ==============================

app = Flask(__name__)


@app.route('/')
def home():
    return "Bot de impresion activo"


def run_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ==============================
# TELEGRAM
# ==============================

bot = telebot.TeleBot(TOKEN)

auth_users = set()
user_data = {}


# ==============================
# MENU PRINCIPAL
# ==============================

def show_main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)

    markup.add(
        types.KeyboardButton("IMPRIMIR"),
        types.KeyboardButton("TEST EMAIL")
    )

    bot.send_message(
        chat_id,
        "Menú principal:",
        reply_markup=markup
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
        bot.send_message(chat_id, "Ingrese la contraseña:")


@bot.message_handler(
    func=lambda message: message.chat.id not in auth_users
)
def check_auth(message):
    chat_id = message.chat.id

    if message.content_type == "text" and message.text == PASSWORD:
        auth_users.add(chat_id)
        add_auth_user(chat_id)

        bot.send_message(chat_id, "Acceso concedido.")
        show_main_menu(chat_id)

    else:
        bot.send_message(
            chat_id,
            "Contraseña incorrecta. Ingrese la contraseña:"
        )


# ==============================
# MENU DE OPCIONES
# ==============================

@bot.message_handler(
    func=lambda message:
    message.content_type == "text"
    and message.text in ["IMPRIMIR", "TEST EMAIL"]
)
def handle_menu(message):
    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    if message.text == "IMPRIMIR":
        bot.send_message(chat_id, "Envía la foto a imprimir.")

    elif message.text == "TEST EMAIL":
        bot.send_message(chat_id, "Enviando correo de prueba...")
        send_test_email(chat_id)


# ==============================
# CORREO DE PRUEBA
# ==============================

def send_test_email(chat_id):
    etapa = "Preparando correo"

    try:
        msg = EmailMessage()

        msg["Subject"] = "PING"
        msg["From"] = EMAIL_SENDER
        msg["To"] = TEST_EMAIL

        msg.set_content("ESTE BOT FUNCIONA!")

        etapa = "Conectando con Outlook SMTP"

        with smtplib.SMTP(
            SMTP_SERVER,
            SMTP_PORT,
            timeout=30
        ) as smtp:

            etapa = "Verificando servidor SMTP"
            smtp.ehlo()

            etapa = "Iniciando conexion TLS"
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()

            etapa = "Autenticando cuenta Outlook"

            smtp.login(
                EMAIL_SENDER,
                EMAIL_PASSWORD
            )

            etapa = "Enviando correo a Gmail"

            smtp.send_message(
                msg,
                from_addr=EMAIL_SENDER,
                to_addrs=[TEST_EMAIL]
            )

        bot.send_message(
            chat_id,
            "✅ CORREO ENVIADO\n\n"
            "Destinatario: " + TEST_EMAIL + "\n"
            "Asunto: PING\n"
            "Mensaje: ESTE BOT FUNCIONA!\n\n"
            "Outlook aceptó el envío."
        )

    except smtplib.SMTPAuthenticationError as e:
        bot.send_message(
            chat_id,
            "❌ ERROR DE AUTENTICACION OUTLOOK\n\n"
            "Codigo SMTP: " + str(e.smtp_code) + "\n"
            "Respuesta: " + str(e.smtp_error) + "\n\n"
            "Revisa:\n"
            "• Correo y contraseña de aplicacion.\n"
            "• Que la contraseña no tenga espacios adicionales.\n"
            "• Que la verificacion en dos pasos siga activa.\n"
            "• Que SMTP AUTH este habilitado."
        )

    except smtplib.SMTPRecipientsRefused as e:
        bot.send_message(
            chat_id,
            "❌ DESTINATARIO RECHAZADO\n\n"
            "Respuesta SMTP:\n" + str(e.recipients)
        )

    except smtplib.SMTPResponseException as e:
        bot.send_message(
            chat_id,
            "❌ ERROR DEL SERVIDOR SMTP\n\n"
            "Codigo: " + str(e.smtp_code) + "\n"
            "Respuesta: " + str(e.smtp_error)
        )

    except smtplib.SMTPConnectError as e:
        bot.send_message(
            chat_id,
            "❌ ERROR DE CONEXION SMTP\n\n" + str(e)
        )

    except smtplib.SMTPServerDisconnected as e:
        bot.send_message(
            chat_id,
            "❌ OUTLOOK DESCONECTO LA SESION\n\n" + str(e)
        )

    except (socket.gaierror, TimeoutError, OSError) as e:
        bot.send_message(
            chat_id,
            "❌ ERROR DE RED O CONEXION\n\n"
            "Tipo: " + type(e).__name__ + "\n"
            "Detalle: " + str(e)
        )

    except ssl.SSLError as e:
        bot.send_message(
            chat_id,
            "❌ ERROR DE SEGURIDAD TLS\n\n" + str(e)
        )

    except Exception as e:
        bot.send_message(
            chat_id,
            "❌ ERROR INESPERADO\n\n"
            "Etapa: " + etapa + "\n"
            "Tipo: " + type(e).__name__ + "\n"
            "Detalle: " + str(e)
        )

# ==============================
# RECIBIR FOTOGRAFIAS
# ==============================

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    chat_id = message.chat.id

    if chat_id not in auth_users:
        return

    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)

    user_data[chat_id] = {
        "photo": downloaded_file
    }

    markup = types.InlineKeyboardMarkup()

    markup.add(
        types.InlineKeyboardButton(
            "Color",
            callback_data="print_color"
        ),
        types.InlineKeyboardButton(
            "Blanco y Negro",
            callback_data="print_bw"
        )
    )

    bot.send_message(
        chat_id,
        "Seleccione el modo de impresión:",
        reply_markup=markup
    )


# ==============================
# BOTONES INLINE
# ==============================

@bot.callback_query_handler(
    func=lambda call: True
)
def handle_callbacks(call):
    chat_id = call.message.chat.id

    bot.answer_callback_query(call.id)

    if chat_id not in auth_users:
        return

    if call.data in ["print_color", "print_bw"]:

        is_color = call.data == "print_color"

        photo_bytes = user_data.get(
            chat_id, {}
        ).get("photo")

        if photo_bytes:
            bot.send_message(
                chat_id,
                "Procesando imagen..."
            )

            send_to_printer(
                chat_id,
                photo_bytes,
                is_color
            )

            user_data.pop(chat_id, None)

        else:
            bot.send_message(
                chat_id,
                "Error: No se encontró la foto. Vuelve a enviarla."
            )

        show_main_menu(chat_id)


# ==============================
# ENVIO A IMPRESORA BROTHER
# ==============================

def send_to_printer(chat_id, photo_bytes, is_color):
    try:
        bot.send_message(
            chat_id,
            "Enviando mensaje a printy..."
        )

        img = Image.open(BytesIO(photo_bytes))

        if not is_color:
            img = img.convert("L")
        else:
            img = img.convert("RGB")

        img = img.resize(
            (1240, 1754),
            Image.Resampling.LANCZOS
        )

        output = BytesIO()

        img.save(
            output,
            format="JPEG",
            quality=95
        )

        image_data = output.getvalue()

        msg = EmailMessage()

        msg["Subject"] = "Impresion desde Telegram"
        msg["From"] = EMAIL_SENDER
        msg["To"] = PRINTER_EMAIL

        msg.set_content(
            "Documento enviado desde el bot de Telegram."
        )

        msg.add_attachment(
            image_data,
            maintype="image",
            subtype="jpeg",
            filename="impresion.jpg"
        )

        with smtplib.SMTP(
            SMTP_SERVER,
            SMTP_PORT,
            timeout=30
        ) as smtp:

            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()

            smtp.login(
                EMAIL_SENDER,
                EMAIL_PASSWORD
            )

            smtp.send_message(
                msg,
                from_addr=EMAIL_SENDER,
                to_addrs=[PRINTER_EMAIL]
            )

        bot.send_message(
            chat_id,
            "Estado de envio: ¡Exitoso! Se imprimió correctamente."
        )

    except Exception as e:
        bot.send_message(
            chat_id,
            f"Error en el estado de envio: {str(e)}"
        )


# ==============================
# INICIO
# ==============================

if __name__ == "__main__":

    init_db()

    auth_users = get_auth_users()

    server_thread = threading.Thread(
        target=run_server,
        daemon=True
    )

    server_thread.start()

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
