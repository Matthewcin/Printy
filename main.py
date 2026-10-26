import telebot
from telebot import types
from PIL import Image
from io import BytesIO
import smtplib
from email.message import EmailMessage

TOKEN = "8837741358:AAFckJCvuACBT4-KIyNp7WqxQ69YEQEYSXY"
PASSWORD = "160620"
PRINTER_EMAIL = "yellower@print.brother.com"
EMAIL_SENDER = "printythebot@outlook.com"
EMAIL_PASSWORD = "lybjezesstkmwmzb"
SMTP_SERVER = "smtp-mail.outlook.com"
SMTP_PORT = 587

bot = telebot.TeleBot(TOKEN)
auth_users = set()
user_states = {}
user_data = {}

@bot.message_handler(commands=["start"])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id in auth_users:
        show_main_menu(chat_id)
    else:
        user_states[chat_id] = "WAITING_PASSWORD"
        bot.send_message(chat_id, "Ingrese la contraseña:")

@bot.message_handler(func=lambda message: message.chat.id not in auth_users and user_states.get(message.chat.id) == "WAITING_PASSWORD")
def check_password(message):
    chat_id = message.chat.id
    if message.text == PASSWORD:
        auth_users.add(chat_id)
        user_states[chat_id] = "MAIN_MENU"
        bot.send_message(chat_id, "Acceso concedido.")
        show_main_menu(chat_id)
    else:
        bot.send_message(chat_id, "Contraseña incorrecta.")

def show_main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("IMPRIMIR"), types.KeyboardButton("INFORMACION"), types.KeyboardButton("CONFIGURACION"))
    bot.send_message(chat_id, "Menú principal:", reply_markup=markup)

@bot.message_handler(func=lambda message: message.chat.id in auth_users and message.text in ["IMPRIMIR", "INFORMACION", "CONFIGURACION"])
def handle_menu(message):
    chat_id = message.chat.id
    if message.text == "IMPRIMIR":
        user_states[chat_id] = "WAITING_PHOTO"
        bot.send_message(chat_id, "Envía la foto a imprimir.")
    elif message.text == "INFORMACION":
        get_printer_info(chat_id)
    elif message.text == "CONFIGURACION":
        show_config_menu(chat_id)

@bot.message_handler(content_types=["photo"], func=lambda message: message.chat.id in auth_users and user_states.get(message.chat.id) == "WAITING_PHOTO")
def handle_photo(message):
    chat_id = message.chat.id
    file_info = bot.get_file(message.photo[-1].file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    user_data[chat_id] = {"photo": downloaded_file}
    
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Color", callback_data="print_color"), types.InlineKeyboardButton("Blanco y Negro", callback_data="print_bw"))
    bot.send_message(chat_id, "Seleccione el modo de impresión:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["print_color", "print_bw"])
def process_print(call):
    chat_id = call.message.chat.id
    is_color = call.data == "print_color"
    photo_bytes = user_data.get(chat_id, {}).get("photo")
    
    if photo_bytes:
        bot.answer_callback_query(call.id, "Procesando...")
        send_to_printer(chat_id, photo_bytes, is_color)
    else:
        bot.answer_callback_query(call.id, "Error con la foto.")
    
    user_states[chat_id] = "MAIN_MENU"
    show_main_menu(chat_id)

def send_to_printer(chat_id, photo_bytes, is_color):
    try:
        img = Image.open(BytesIO(photo_bytes))
        if not is_color:
            img = img.convert("L")
        else:
            img = img.convert("RGB")
        
        img = img.resize((2480, 3508), Image.Resampling.LANCZOS)
        
        output = BytesIO()
        img.save(output, format="JPEG", quality=95)
        image_data = output.getvalue()
        
        msg = EmailMessage()
        msg['Subject'] = 'Impresion desde Telegram'
        msg['From'] = EMAIL_SENDER
        msg['To'] = PRINTER_EMAIL
        msg.set_content("Documento enviado desde el bot de Telegram.")
        
        msg.add_attachment(image_data, maintype='image', subtype='jpeg', filename='impresion.jpg')
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
            
        bot.send_message(chat_id, "Impresión enviada correctamente al correo de la impresora.")
    except Exception as e:
        bot.send_message(chat_id, f"Error al enviar el correo: {str(e)}")

def get_printer_info(chat_id):
    bot.send_message(chat_id, "Estado: Conectada\nUptime: Calculando...\nTinta Cyan: ?\nTinta Magenta: ?\nTinta Yellow: ?\nTinta Black: ?\n\nEsperando modelo Brother para implementar lectura SNMP.")

def show_config_menu(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Modo Ahorro", callback_data="cfg_ahorro"), types.InlineKeyboardButton("Limpieza de Cabezales", callback_data="cfg_limpieza"))
    bot.send_message(chat_id, "Opciones de configuración (Esperando modelo):", reply_markup=markup)

bot.polling()
