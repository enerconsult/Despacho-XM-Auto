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

def generar_dashboard(records, target_date_str, avg_str):
    import matplotlib.pyplot as plt
    try:
        horas = [f"{str(h-1).zfill(2)}-{str(h).zfill(2)}h" for h, _ in records]
        valores = [v for _, v in records]
        
        plt.style.use('dark_background')
        fig = plt.figure(figsize=(16, 8), facecolor='#1a1b26')
        gs = fig.add_gridspec(1, 2, width_ratios=[2.5, 1])
        
        ax1 = fig.add_subplot(gs[0, 0], facecolor='#1a1b26')
        ax1.plot(horas, valores, color='#7aa2f7', linewidth=3, marker='o', markersize=8)
        ax1.fill_between(horas, valores, color='#7aa2f7', alpha=0.2)
        ax1.set_title(f'Predespacho XM - {target_date_str} (Promedio: $ {avg_str}/kWh)', fontsize=18, color='#c0caf5', pad=20)
        ax1.set_ylabel('Costo Marginal (COP/kWh)', fontsize=14, color='#c0caf5')
        ax1.tick_params(axis='x', rotation=45, colors='#c0caf5')
        ax1.tick_params(axis='y', colors='#c0caf5')
        ax1.grid(color='#414868', linestyle='--', alpha=0.5)
        ax1.spines['top'].set_visible(False)
        ax1.spines['right'].set_visible(False)
        ax1.spines['bottom'].set_color('#414868')
        ax1.spines['left'].set_color('#414868')
        
        ax2 = fig.add_subplot(gs[0, 1], facecolor='#1a1b26')
        ax2.axis('off')
        table_data = [[h, f"$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")] for h, v in zip(horas, valores)]
        col_labels = ['Periodo', 'COP/kWh']
        table = ax2.table(cellText=table_data, colLabels=col_labels, loc='center', cellLoc='center')
        table.auto_set_font_size(False)
        table.set_fontsize(12)
        table.scale(1, 1.8)
        for (i, j), cell in table.get_celld().items():
            cell.set_edgecolor('#414868')
            if i == 0:
                cell.set_facecolor('#24283b')
                cell.set_text_props(weight='bold', color='#7aa2f7')
            else:
                cell.set_facecolor('#1a1b26')
                cell.set_text_props(color='#c0caf5')
                
        plt.tight_layout()
        img_path = os.path.join(BASE_DIR, "Dashboard_Predespacho.png")
        plt.savefig(img_path, dpi=150, bbox_inches='tight', facecolor='#1a1b26')
        plt.close()
        
        import time
        ts = int(time.time())
        return f"https://raw.githubusercontent.com/enerconsult/Despacho-XM-Auto/main/Dashboard_Predespacho.png?v={ts}"
    except Exception as e:
        print(f"Error generating dashboard: {e}")
        return ""

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

    converted_records = [(hora, float(valor) / 1000.0) for hora, valor in records]
    avg_val = sum(v for h, v in converted_records) / len(converted_records) if converted_records else 0
    avg_str = f"{avg_val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    img_url = generar_dashboard(converted_records, target_date_str, avg_str)

    # Ahora sí, subir a GitHub porque la nueva imagen ya se generó
    import os
    try:
        print("Commiting and pushing changes to GitHub to host the new image...")
        os.system('git config --local user.email "action@github.com"')
        os.system('git config --local user.name "GitHub Action"')
        os.system('git add -A')
        os.system('git commit -m "✅ Actualizacion + Dashboard visual"')
        os.system('git push')
        import time
        time.sleep(5)  # Dar margen a que los CDNs de GitHub indexen el nuevo archivo
    except Exception as e:
        print("Git push error:", e)

    msg = f"🟢 *Enerconsult - Predespacho XM*\n"
    msg += f"📅 {target_date_str} - Promedio: $ {avg_str}/kWh\n\n"
    
    for hora, valor in converted_records:
        start_hour = str(hora - 1).zfill(2)
        end_hour = str(hora).zfill(2)
        val_str = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        msg += f"🕒 {start_hour}-{end_hour}h: $ {val_str}\n"

    webhook_url = "https://hook.us2.make.com/k2gh8wq6gimstrabg61p6ked7ahxxgjv"
    
    payload = {
        "texto": msg,
        "image_url": img_url
    }
        
    data = json.dumps(payload).encode('utf-8')
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
    
    dt = tomorrow
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
    else:
        print(f"Files not available for {target_date_str}. Sending error hook...")
        import json
        msg = f"⚠️ *Alerta Enerconsult*\nEl portal XM aún no ha publicado los archivos completos de Predespacho Ideal para mañana ({target_date_str}) o sus servidores están temporalmente caídos."
        webhook_url = "https://hook.us2.make.com/k2gh8wq6gimstrabg61p6ked7ahxxgjv"
        
        # Enviamos un icono de alerta público de Wikipedia para engañar a GreenAPI
        # ya que exige estrictamente la existencia de urlFile cuando usamos el módulo "Send File by URL".
        payload = {
            "texto": msg,
            "image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/1/17/Warning.svg/512px-Warning.svg.png"
        }
            
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(webhook_url, data=data, headers={'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(req)
            print("Error notification sent.")
        except Exception as e:
            print(f"Error sending error webhook: {e}")

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
