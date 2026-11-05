# Copyright (c) 2026, Quantbit Technolgies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
import frappe
import zk
from frappe.model.document import Document
from datetime import datetime, timedelta
import re
from frappe.utils import now, getdate

import glob
import json
import os
import shutil
import subprocess


# =====================================================================
#  eSSL FACE MACHINE (zkemkeeper SDK through Wine + Windows Python)
# =====================================================================
FACE_SYNC_TIMEOUT = 900
FACE_DEFAULT_MACHINE_NUMBER = 1
FACE_CONNECT_RETRIES = 3

FACE_WORKER_SCRIPT = r'''
import sys, json, time

def out(d):
    print(json.dumps(d))
    sys.stdout.flush()

try:
    import comtypes.client
except Exception as e:
    out({"status": "error", "err": "comtypes not available in Windows Python: %s" % e}); sys.exit(0)

ip         = sys.argv[1].strip()
port       = int(sys.argv[2])
comm_key   = sys.argv[3].strip()
dev_no     = int(sys.argv[4])
start_date = sys.argv[5]
end_date   = sys.argv[6]
retries    = int(sys.argv[7]) if len(sys.argv) > 7 else 3

try:
    z = comtypes.client.CreateObject("zkemkeeper.ZKEM.1")
except Exception as e:
    out({"status": "error", "err": "zkemkeeper not registered: %s" % e}); sys.exit(0)

connected = False
errors = []
for attempt in range(retries):
    try: z.SetCommPassword(int(comm_key) if comm_key.isdigit() else 0)
    except Exception: pass
    try: z.SetCommPasswordEx(comm_key)
    except Exception: pass

    if z.Connect_Net(ip, port):
        connected = True
        break

    try:
        e = z.GetLastError(0)
        e = e[0] if isinstance(e, (list, tuple)) else e
    except Exception:
        e = "unknown"
    errors.append(e)
    try: z.Disconnect()
    except Exception: pass
    time.sleep(3)

if not connected:
    out({"status": "error",
         "err": "Connect_Net failed after %d attempts, error codes: %s" % (retries, errors)})
    sys.exit(0)

logs = []
try:
    z.EnableDevice(dev_no, False)
    z.ReadGeneralLogData(dev_no)
    while True:
        r = z.SSR_GetGeneralLogData(dev_no)
        if not r[-1]:
            break
        ts = "%04d-%02d-%02d %02d:%02d:%02d" % (r[3], r[4], r[5], r[6], r[7], r[8])
        if start_date <= ts[:10] <= end_date:
            logs.append([str(r[0]).strip(), ts])
finally:
    try: z.EnableDevice(dev_no, True)
    except Exception: pass
    try: z.Disconnect()
    except Exception: pass

out({"status": "success", "logs": logs})
'''


def _clean_face_params(machine_ip, common_key, port):
    ip = str(machine_ip or "").strip()

    try:
        port = int(float(str(port).strip())) if port not in (None, "") else 4370
    except ValueError:
        port = 4370

    key = "" if common_key is None else str(common_key).strip()
    if key.endswith(".0") and key[:-2].isdigit():
        key = key[:-2]
    if not key:
        key = "0"

    return ip, port, key


def find_wine_python():
    prefixes = []
    for p in (frappe.conf.get("face_machine_wine_prefix"),
              os.environ.get("WINEPREFIX"), "~/.wine-zk", "~/.wine"):
        if p:
            p = os.path.abspath(os.path.expanduser(p))
            if p not in prefixes:
                prefixes.append(p)

    checked = []
    for prefix in prefixes:
        drive_c = os.path.join(prefix, "drive_c")
        if not os.path.isdir(drive_c):
            checked.append(f"{prefix}  (prefix not found)")
            continue

        candidates = []
        for ver in ("Python311", "Python312", "Python310", "Python39", "Python38"):
            candidates += [
                os.path.join(drive_c, ver, "python.exe"),
                os.path.join(drive_c, "Program Files", ver, "python.exe"),
            ]
            candidates += glob.glob(os.path.join(
                drive_c, "users", "*", "AppData", "Local", "Programs", "Python", ver, "python.exe"))
        candidates += [
            c for c in glob.glob(os.path.join(drive_c, "**", "python.exe"), recursive=True)
            if not any(x in c.lower() for x in ("venv", "(x86)", "windowsapps", "test"))
        ]

        for c in candidates:
            checked.append(c)
            if os.path.isfile(c):
                rel = os.path.relpath(c, drive_c)
                return prefix, c, "C:\\" + rel.replace("/", "\\"), checked

    return None, None, None, checked


def get_face_attendance_data(start_date, end_date, machine_ip, common_key, port,
                             machine_code, machine_number=None):
    ip, port, key = _clean_face_params(machine_ip, common_key, port)
    if not ip:
        frappe.throw(f"Machine IP is empty for face machine {machine_code}")

    start_str = getdate(start_date).strftime("%Y-%m-%d")
    end_str = getdate(end_date).strftime("%Y-%m-%d")
    machine_number = int(machine_number or FACE_DEFAULT_MACHINE_NUMBER)

    wine_bin = shutil.which("wine") or "/usr/bin/wine"
    if not os.path.exists(wine_bin):
        frappe.throw(f"Wine is not installed on the server (machine {machine_code})")

    wine_prefix, _win_py_linux, win_py, checked = find_wine_python()
    if not win_py:
        frappe.log_error("\n".join(checked[:40]), "Face Machine: Windows Python not found")
        frappe.throw(f"Windows Python not found in any Wine prefix (machine {machine_code}). "
                     f"Check Error Log for searched paths.")

    sys32 = os.path.join(wine_prefix, "drive_c", "windows", "system32")
    if not os.path.isdir(sys32):
        frappe.throw(f"Wine system32 directory not found at: {sys32}")
    if not os.path.exists(os.path.join(sys32, "tcpcomm.dll")) and \
            os.path.exists(os.path.join(sys32, "commsTcp.dll")):
        shutil.copyfile(os.path.join(sys32, "commsTcp.dll"), os.path.join(sys32, "tcpcomm.dll"))

    home = os.environ.get("HOME") or os.path.expanduser("~")
    wine_env = {**os.environ, "WINEPREFIX": wine_prefix, "WINEDEBUG": "-all", "HOME": home}
    wine_env.pop("DISPLAY", None)

    cmd = [wine_bin, win_py, "-c", FACE_WORKER_SCRIPT,
           ip, str(port), key, str(machine_number),
           start_str, end_str, str(FACE_CONNECT_RETRIES)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=wine_env,
                              cwd=home, timeout=FACE_SYNC_TIMEOUT)
    except subprocess.TimeoutExpired:
        frappe.throw(f"Face machine {machine_code} ({ip}:{port}) timed out after {FACE_SYNC_TIMEOUT}s")
    except Exception as e:
        frappe.throw(f"Could not start Wine for face machine {machine_code}: {e}")

    data = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue

    if not data or data.get("status") != "success":
        err = (data.get("err") if data else None) or (proc.stderr or "").strip() \
            or (proc.stdout or "").strip() or f"Wine Python exited with code {proc.returncode}"
        frappe.throw(
            f"There is a connection issue with face machine {machine_code} "
            f"(IP {ip!r}, port {port}, comm key {key!r}, prefix {wine_prefix}): {err}"
        )

    normalized = []
    for uid, ts in data.get("logs", []):
        uid = str(uid).strip()
        if not uid:
            continue
        if uid.isdigit():
            uid = str(int(uid))
        normalized.append((uid, ts))

    normalized.sort(key=lambda x: (x[0], x[1]))

    attendance_records = {}
    for uid, ts in normalized:
        dict_key = f"{ts[:10]}-{uid}"
        if dict_key not in attendance_records:
            attendance_records[dict_key] = [uid, ts]
        else:
            attendance_records[dict_key].append(ts)

    return attendance_records


# =====================================================================
#  EXISTING CODE (unchanged except machine-type selection in sync_data)
# =====================================================================
class BiometricAttendance(Document):
	@frappe.whitelist()
	def check_dates(self):
		if(self.to_date and self.from_date):
			if(self.to_date < self.from_date):
				frappe.throw("From Date can not greater than To Date")
    
	@frappe.whitelist()
	def get_current_date(self):
		self.from_date=datetime.today()
		self.to_date=datetime.today()
	
 
	def check_diff(self,in_time,out_time):
		in_time = datetime.strptime(in_time, '%Y-%m-%d %H:%M:%S')
		out_time = datetime.strptime(out_time, '%Y-%m-%d %H:%M:%S')
		diff = out_time - in_time
		if diff <= timedelta(minutes=15):
			out_time = None
		return out_time


	def get_attendance_data(self, start_date, end_date, machine_ip, common_key, port, machine_code):    
		zk_instance = zk.base.ZK(machine_ip, port=int(port), timeout=60, password=str(common_key))
        
		conn = ""
		try:
			conn = zk_instance.connect()
			if conn:
				attendance_data = conn.get_attendance()
				conn.disable_device()
				pattern = r"<Attendance>: (\d+) : (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \((\d+), (\d+)\)"
				attendance_records = {}
				
				if isinstance(start_date, str):
					start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
				if isinstance(end_date, str):
					end_date = datetime.strptime(end_date, "%Y-%m-%d").date()

				for record in attendance_data:
					record_date = record.timestamp.date()

					if start_date <= record_date <= end_date:
						attendance_string = record
						match = re.match(pattern, str(attendance_string))
						if match:
							user_id = int(match.group(1))
							timestamp = match.group(2)
							dict_key = f"{record_date.strftime('%Y-%m-%d')}-{user_id}"
							if dict_key not in attendance_records:
								attendance_records[dict_key] = [str(user_id), timestamp]
							else:
								attendance_records[dict_key].append(timestamp)
				return attendance_records

		except Exception as e:
			frappe.throw(f"There is a connection issue with machine {machine_code}: {e}")
		finally:
			if conn:
				conn.disconnect()


@frappe.whitelist()
def sync_data(docname):
    def log(msg):
        frappe.log_error("Biometric Sync Debug", str(msg))

    log(f"Started sync for doc: {docname}")

    self = frappe.get_doc("Biometric Attendance", docname)
    if not (self.from_date and self.to_date):
        frappe.throw("Please select from date and to date")

    self.check_dates()

    employees = frappe.get_all(
        "Employee",
        filters={"status": "Active"},
        fields=["name", "attendance_device_id"]
    )
    log(f"Loaded {len(employees)} active employees")
    device_to_emp = {}
    for e in employees:
        if e.get("attendance_device_id"):
            device_to_emp[str(e.attendance_device_id)] = e.name
    log(f"Prepared device_to_emp map: {len(device_to_emp)} entries")

    shift_type_rows = frappe.get_all(
        "Shift Type",
        fields=["name", "start_time", "end_time", "begin_check_in_before_shift_start_time",
                "allow_check_out_after_shift_end_time", "custom_next_day", "custom_shift_type"]
    )
    log(f"Loaded {len(shift_type_rows)} shift types")
    shift_type_cache = {}
    for r in shift_type_rows:
        shift_type_cache[r["name"]] = (
            r.get("start_time"),
            r.get("end_time"),
            r.get("begin_check_in_before_shift_start_time") or 0,
            r.get("allow_check_out_after_shift_end_time") or 0,
            r.get("custom_next_day"),
            r.get("custom_shift_type")
        )

    assignments = frappe.get_all(
        "Shift Assignment",
        filters=[["docstatus", "=", 1],
                 ["start_date", "<=", self.to_date],
                 ["end_date", ">=", self.from_date]],
        fields=["employee", "shift_type", "start_date", "end_date"]
    )
    assignments_by_emp = {}
    for a in assignments:
        assignments_by_emp.setdefault(a["employee"], []).append(a)
    log(f"Total employees mapped to devices: {len(assignments)}")

    def get_shift_for_emp_on(emp, req_date_str):
        if not emp or emp not in assignments_by_emp:
            return None
        req_date = datetime.strptime(req_date_str, "%Y-%m-%d").date()
        for a in assignments_by_emp[emp]:
            s = datetime.strptime(str(a["start_date"]), "%Y-%m-%d").date()
            e = datetime.strptime(str(a["end_date"]), "%Y-%m-%d").date()
            if s <= req_date <= e:
                return a["shift_type"]
        return None

    all_emp_names = list({name for name in device_to_emp.values()})
    existing_checkins_set = set()
    if all_emp_names:
        rows = frappe.get_all(
            "Employee Checkin",
            filters=[["employee", "in", all_emp_names],
                     ["time", ">=", str(self.from_date) + " 00:00:00"],
                     ["time", "<=", str(self.to_date) + " 23:59:59"]],
            fields=["employee", "time", "log_type"]
        )
        for r in rows:
            existing_checkins_set.add((r["employee"], str(r["time"]), r["log_type"]))

    new_checkins = []

    def parse_dt_str(dt_str):
        if isinstance(dt_str, datetime):
            return dt_str
        return datetime.strptime(str(dt_str), "%Y-%m-%d %H:%M:%S")

    for machine in self.get("machine_configuration"):
        if not machine.check:
            continue

        if machine.get("face_attendance_machine"):
            log(f"Fetching FACE machine {machine.machine_code} ({machine.machine_ip})")
            try:
                raw_attendance = get_face_attendance_data(
                    self.from_date, self.to_date,
                    machine.machine_ip, machine.com_key,
                    machine.port_no, machine.machine_code,
                    machine.get("machine_number")
                )
            except Exception:
                frappe.log_error(frappe.get_traceback(),
                                 f"Biometric Sync: face machine {machine.machine_code} failed")
                continue
            log(f"Face machine {machine.machine_code}: {len(raw_attendance or {})} employee-day groups")
        else:
            raw_attendance = self.get_attendance_data(self.from_date, self.to_date,
                                                      machine.machine_ip, machine.com_key,
                                                      machine.port_no, machine.machine_code)

        for key, times in (raw_attendance or {}).items():
            if times == '0':
                continue

            device_id = str(times[0])
            emp = device_to_emp.get(device_id)
            if not emp:
                continue

            tdate = times[1]
            date_only = parse_dt_str(tdate).date()
            req_date = date_only.strftime("%Y-%m-%d")

            shift_name = get_shift_for_emp_on(emp, req_date)
            if not shift_name:
                continue

            shift_details = shift_type_cache.get(shift_name)
            if not shift_details:
                continue

            (start_time, end_time,
             begin_check_in_before_shift,
             allow_check_out_after_shift,
             next_day, custom_shift_type) = shift_details

            start_time_dt = datetime.strptime(str(start_time), "%H:%M:%S")
            end_time_dt = datetime.strptime(str(end_time), "%H:%M:%S")

            begin_td = timedelta(minutes=int(begin_check_in_before_shift or 0))
            allow_td = timedelta(minutes=int(allow_check_out_after_shift or 0))

            shift_from_dt = (start_time_dt - begin_td)
            shift_to_dt = (end_time_dt + allow_td)
            to_date_checkin_dt = (end_time_dt - allow_td - begin_td)

            shift_in_time = shift_from_dt.time()
            shift_out_time = shift_to_dt.time()
            time_to_checkin = to_date_checkin_dt.time()

            in_time = None
            out_time = None

            if custom_shift_type == "Third":
                if len(times) == 2:
                    time_val = parse_dt_str(times[1])
                    if time_val.time() <= time_to_checkin:
                        in_time = times[1]
                    else:
                        out_time = times[1]
                else:
                    in_time = times[1]
                    out_time = times[-1]
                    out_time = self.check_diff(in_time, out_time)
                    if not out_time:
                        tv = parse_dt_str(times[1])
                        if tv.time() < time_to_checkin:
                            in_time = times[1]
                            out_time = None
                        else:
                            out_time = times[-1]
                            in_time = None

            elif custom_shift_type == "Second":
                if len(times) == 2:
                    tv = parse_dt_str(times[1])
                    if tv.time() <= time_to_checkin:
                        in_time = times[1]
                    else:
                        out_time = times[1]
                else:
                    in_time = times[1]
                    out_time = times[-1]
                    out_time = self.check_diff(in_time, out_time)
                    if not out_time:
                        tv = parse_dt_str(times[1])
                        if tv.time() < time_to_checkin:
                            in_time = times[1]
                            out_time = None
                        else:
                            out_time = times[-1]
                            in_time = None

            elif custom_shift_type == "Fourth":
                if len(times) == 2:
                    tv = parse_dt_str(times[1])
                    if shift_in_time < tv.time() < datetime.strptime("23:59:59", "%H:%M:%S").time():
                        in_time = times[1]
                    else:
                        out_time = times[1]
                else:
                    min_diff_out = float("inf")
                    min_diff_in = float("inf")
                    chosen_out = None
                    chosen_in = None
                    for ts in times[1:]:
                        ts_obj = parse_dt_str(ts)
                        ts_time = ts_obj.time()

                        diff_out = abs((ts_time.hour - shift_out_time.hour) * 3600 +
                                       (ts_time.minute - shift_out_time.minute) * 60 +
                                       (ts_time.second - shift_out_time.second))
                        diff_in = abs((ts_time.hour - shift_in_time.hour) * 3600 +
                                      (ts_time.minute - shift_in_time.minute) * 60 +
                                      (ts_time.second - shift_in_time.second))

                        if diff_out < min_diff_out:
                            min_diff_out = diff_out
                            chosen_out = ts_obj
                        if diff_in < min_diff_in:
                            min_diff_in = diff_in
                            chosen_in = ts_obj

                    if chosen_out:
                        out_time = chosen_out.strftime("%Y-%m-%d %H:%M:%S")
                    if chosen_in:
                        in_time = chosen_in.strftime("%Y-%m-%d %H:%M:%S")

            else:
                if len(times) == 2:
                    tv = parse_dt_str(times[1])
                    if tv.time() <= time_to_checkin:
                        in_time = times[1]
                    else:
                        out_time = times[1]
                else:
                    in_time = times[1]
                    out_time = times[-1]
                    out_time = self.check_diff(in_time, out_time)
                    if not out_time:
                        tv = parse_dt_str(times[1])
                        if tv.time() < time_to_checkin:
                            in_time = times[1]
                            out_time = None
                        else:
                            out_time = times[-1]
                            in_time = None

            def normalize_and_prep(check_time_str, log_type):
                if not check_time_str:
                    return None
                dt_obj = parse_dt_str(check_time_str)
                dt_s = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
                key = (emp, dt_s, log_type)
                if key in existing_checkins_set:
                    return None
                existing_checkins_set.add(key)
                return {
                    "employee": emp,
                    "time": dt_s,
                    "device_id": machine.machine_code,
                    "log_type": log_type
                }

            cand_in = normalize_and_prep(in_time, "IN")
            cand_out = normalize_and_prep(out_time, "OUT")

            if cand_in:
                d = frappe.get_doc({
                    "doctype": "Employee Checkin",
                    "employee": cand_in["employee"],
                    "time": cand_in["time"],
                    "device_id": cand_in["device_id"],
                    "log_type": "IN"
                })
                new_checkins.append(d)

            if cand_out:
                d = frappe.get_doc({
                    "doctype": "Employee Checkin",
                    "employee": cand_out["employee"],
                    "time": cand_out["time"],
                    "device_id": cand_out["device_id"],
                    "log_type": "OUT"
                })
                new_checkins.append(d)

    for doc in new_checkins:
        try:
            doc.insert(ignore_permissions=True)
            log(f"Inserting Checkin ENtry")
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "Biometric Sync: insert checkin error")

    frappe.db.commit()

    return {"inserted": len(new_checkins)}


@frappe.whitelist()
def enqueue_details(docname):
    frappe.enqueue(
        method="face_biometric_integration.face_biometric_integration.face_biometric_integration.doctype.biometric_attendance.biometric_attendance.run_background",
        queue="short",
        timeout=5000,
        is_async=True,
        docname=docname,
    )
    frappe.msgprint("Biometric Data Processing Please Check After Some time...")

    
@frappe.whitelist()
def run_background(docname):
    sync_data(docname)


def debug_face_machine(ip, port=4370, comm_key="0", machine_number=1, from_date=None, to_date=None):
    from frappe.utils import today
    from_date = from_date or today()
    to_date = to_date or from_date
    data = get_face_attendance_data(from_date, to_date, ip, comm_key, port, "DEBUG", machine_number)
    return {"employee_day_groups": len(data), "sample": list(data.items())[:5]}
