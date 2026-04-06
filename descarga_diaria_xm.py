import os
import sqlite3
import datetime
import urllib.request
import csv
import ssl
from io import StringIO

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "XM_Data.db")
BASE_URL = "https://api-portalxm.xm.com.co/administracion-archivos/ficheros/descarga-archivo?ruta=M:/InformacionAgentes/Usuarios/Publico/PredespachoIdeal/{year_month}/{filename}&nombreBlobContainer=storageportalxm"
HEADERS = {'User-Agent': 'Mozilla/5.0'}

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS PrId_Data (
            Fecha TEXT,
            Recurso TEXT,
            Hora INTEGER,
            Valor REAL,
            UNIQUE(Fecha, Recurso, Hora)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS iMAR_Data (
            Fecha TEXT,
            Variable TEXT,
            Hora INTEGER,
            Valor REAL,
            UNIQUE(Fecha, Variable, Hora)
        )
    ''')
    conn.commit()
    return conn

def download_file(year_month, filename):
    url = BASE_URL.format(year_month=year_month, filename=filename)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        return None

def process_and_save(conn, file_type, text_content, target_date):
    cursor = conn.cursor()
    f = StringIO(text_content)
    reader = csv.reader(f, delimiter=',')
    
    if file_type == 'PrId':
        data_to_insert = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            recurso = row[0].strip()
            for obj_hr in range(1, 25):
                if len(row) > obj_hr:
                    try:
                        val = float(row[obj_hr].strip())
                    except ValueError:
                        val = 0.0
                    data_to_insert.append((target_date, recurso, obj_hr, val))
        cursor.executemany("INSERT OR REPLACE INTO PrId_Data (Fecha, Recurso, Hora, Valor) VALUES (?, ?, ?, ?)", data_to_insert)
    elif file_type == 'iMAR':
        data_to_insert = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            variable = row[0].strip()
            for obj_hr in range(1, 25):
                if len(row) > obj_hr:
                    try:
                        val = float(row[obj_hr].strip())
                    except ValueError:
                        val = 0.0
                    data_to_insert.append((target_date, variable, obj_hr, val))
        cursor.executemany("INSERT OR REPLACE INTO iMAR_Data (Fecha, Variable, Hora, Valor) VALUES (?, ?, ?, ?)", data_to_insert)
    
    conn.commit()

def send_whatsapp_report(conn, target_date_str):
    import json
    cursor = conn.cursor()
    cursor.execute('''
        SELECT Hora, Valor FROM iMAR_Data 
        WHERE Fecha = ? AND Variable = 'Costo Marginal'
        ORDER BY Hora
    ''', (target_date_str,))
    
    records = cursor.fetchall()
    if not records:
        print("No Costo Marginal data found to send.")
        return

    msg = f"⚡ *Reporte Diario: Predespacho Ideal XM* ⚡\n"
    msg += f"📅 *Fecha:* {target_date_str}\n\n"
    msg += f"*Costo Marginal Estimado (COP/kWh):*\n"
    
    for hora, valor in records:
        start_hour = str(hora - 1).zfill(2)
        end_hour = str(hora).zfill(2)
        # Formato de miles y decimales
        val_str = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        msg += f"🕛 {start_hour}:00 - {end_hour}:00 ➡️ $ {val_str}\n"

    webhook_url = "https://hook.us2.make.com/k2gh8wq6gimstrabg61p6ked7ahxxgjv"
    
    data = json.dumps({"texto": msg}).encode('utf-8')
    req = urllib.request.Request(webhook_url, data=data, headers={'Content-Type': 'application/json'})
    
    try:
        with urllib.request.urlopen(req) as response:
            print("WhatsApp Webhook sent successfully. Make.com response:", response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error sending webhook: {e}")

def run_daily_job():
    conn = init_db()
    
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    
    # Try downloading tomorrow's predispatch first, then fallback to today's
    success = False
    for dt in [tomorrow, today]:
        year_month = dt.strftime("%Y-%m")
        mmdd = dt.strftime("%m%d")
        
        target_date_str = dt.strftime("%Y-%m-%d")
        
        prid_name = f"PrId{mmdd}_NAL.txt"
        imar_name = f"iMAR{mmdd}.txt"
        
        print(f"Trying to download data for schedule {target_date_str}...")
        
        prid_txt = download_file(year_month, prid_name)
        imar_txt = download_file(year_month, imar_name)
        
        if prid_txt and imar_txt:
            print(f"Successfully downloaded both files for {target_date_str}!")
            process_and_save(conn, 'PrId', prid_txt, target_date_str)
            process_and_save(conn, 'iMAR', imar_txt, target_date_str)
            
            with open(os.path.join(BASE_DIR, prid_name), 'w', encoding='utf-8') as f1:
                f1.write(prid_txt)
            with open(os.path.join(BASE_DIR, imar_name), 'w', encoding='utf-8') as f2:
                f2.write(imar_txt)
                
            print("Data saved to SQLite successfully.")
            
            print("Sending WhatsApp report to Make.com...")
            send_whatsapp_report(conn, target_date_str)
            
            success = True
            break
        else:
            print(f"Files not available for {target_date_str}.")

    if not success:
        print("Failed to download any files for today or tomorrow.")
    
    # Verification query
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM PrId_Data")
    prid_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM iMAR_Data")
    imar_count = cursor.fetchone()[0]
    print(f"\nFinal Database Stats -> PrId_Data records: {prid_count} | iMAR_Data records: {imar_count}")
    
    conn.close()

if __name__ == '__main__':
    run_daily_job()
