import pdfplumber
import re

def detect_major_odometer_jumps(pdf_path, drift_threshold=1.0):
    print(f"--- Analyzing {pdf_path} for Odometer Jumps ---")
    
    events = []
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                # Step 1: Extract the date from the top of the page
                page_text = page.extract_text()
                date_match = re.search(r"Record Date:\s*(\d{2}-\d{2}-\d{4})", page_text)
                current_page_date = date_match.group(1) if date_match else "Unknown Date"
                
                # Step 2: Extract the table data
                table = page.extract_table()
                if not table:
                    continue
                
                for row in table[1:]:
                    if len(row) > 5 and row[0] and row[5]:
                        try:
                            odo_value = int(re.sub(r'[^\d]', '', str(row[5])))
                            events.append({
                                'date': current_page_date,
                                'status': row[0].strip().replace('\n', ' '),
                                'time': row[1].strip(),
                                'odo': odo_value
                            })
                        except (ValueError, TypeError):
                            continue

        for i in range(len(events) - 1):
            curr = events[i]
            next_ev = events[i+1]
            
            # Only check jumps if the events are on the same day or sequential
            if curr['status'] in ["OFF DUTY", "SLEEPER BERTH", "ON DUTY"]:
                jump = next_ev['odo'] - curr['odo']
                
                if jump > drift_threshold:
                    print(f"ALARM: Big Odometer Jump Found on {curr['date']}!")
                    print(f"  - Status: {curr['status']} at {curr['time']}")
                    print(f"  - Jump Size: {jump} miles")
                    print(f"  - Changed from {curr['odo']} to {next_ev['odo']}\n")
                elif jump < 0:
                    print(f"WARNING: Odometer decreased by {abs(jump)} miles on {curr['date']} at {curr['time']}\n")

    except Exception as e:
        print(f"Error: {e}")

detect_major_odometer_jumps("logbook.pdf", drift_threshold=1.0)