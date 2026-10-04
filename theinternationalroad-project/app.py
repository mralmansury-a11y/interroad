import os
import io
from functools import wraps
from datetime import date, timedelta

from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    session, send_file, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

from database import get_db, init_db, DB_PATH, log_activity

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-in-production"

ROLE_LABELS = {"admin": "مدير النظام", "manager": "مشرف", "viewer": "اطلاع فقط"}


def ensure_db():
    if not os.path.exists(DB_PATH):
        init_db(seed=True)


@app.before_request
def _before():
    ensure_db()


# ================================================================== أدوات المصادقة والصلاحيات
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("يجب تسجيل الدخول أولاً", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                flash("يجب تسجيل الدخول أولاً", "error")
                return redirect(url_for("login", next=request.path))
            if session.get("role") not in roles:
                flash("ليست لديك صلاحية القيام بهذا الإجراء", "error")
                return redirect(request.referrer or url_for("dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def inject_user():
    return {
        "current_user": {
            "id": session.get("user_id"),
            "username": session.get("username"),
            "full_name": session.get("full_name"),
            "role": session.get("role"),
            "role_label": ROLE_LABELS.get(session.get("role"), ""),
        },
        "current_user_id": session.get("user_id"),
        "can_edit": session.get("role") in ("admin", "manager"),
        "is_admin": session.get("role") == "admin",
    }


# ================================================================== تسجيل الدخول / الخروج
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["role"] = user["role"]
            log_activity(db, username, "تسجيل دخول", "auth")
            db.commit()
            db.close()
            flash(f"مرحبًا بك، {user['full_name']}", "success")
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)

        db.close()
        flash("اسم المستخدم أو كلمة المرور غير صحيحة", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    if session.get("username"):
        db = get_db()
        log_activity(db, session.get("username"), "تسجيل خروج", "auth")
        db.commit()
        db.close()
    session.clear()
    flash("تم تسجيل الخروج بنجاح", "success")
    return redirect(url_for("login"))


# ================================================================== الرئيسية / لوحة المعلومات
@app.route("/")
@login_required
def dashboard():
    db = get_db()
    today = date.today().isoformat()

    sites = db.execute("SELECT * FROM sites ORDER BY km_from").fetchall()

    attendance_today = db.execute(
        """SELECT s.id as site_id, s.name as site_name,
                  COUNT(a.id) as present_count
           FROM sites s
           LEFT JOIN attendance a ON a.site_id = s.id
                AND a.date = ? AND a.status = 'حاضر'
           GROUP BY s.id ORDER BY s.km_from""",
        (today,),
    ).fetchall()

    total_workers = db.execute("SELECT COUNT(*) c FROM workers WHERE active=1").fetchone()["c"]
    present_today = db.execute(
        "SELECT COUNT(*) c FROM attendance WHERE date=? AND status='حاضر'", (today,)
    ).fetchone()["c"]

    maintenance_due = db.execute(
        """SELECT e.*, s.name as site_name FROM equipment e
           LEFT JOIN sites s ON s.id = e.site_id
           WHERE (e.operating_hours - e.hours_at_last_maint) >= e.maintenance_interval"""
    ).fetchall()

    low_stock = db.execute(
        """SELECT ms.*, m.name as material_name, m.unit, m.min_threshold, s.name as site_name
           FROM material_stock ms
           JOIN materials m ON m.id = ms.material_id
           JOIN sites s ON s.id = ms.site_id
           WHERE ms.quantity < m.min_threshold
           ORDER BY ms.quantity ASC"""
    ).fetchall()

    pending_transfers = db.execute(
        """SELECT t.*, m.name as material_name, m.unit,
                  sf.name as from_name, st.name as to_name
           FROM material_transfers t
           JOIN materials m ON m.id = t.material_id
           LEFT JOIN sites sf ON sf.id = t.from_site_id
           JOIN sites st ON st.id = t.to_site_id
           WHERE t.status = 'قيد الانتظار'
           ORDER BY t.date DESC"""
    ).fetchall()

    # -------- بيانات الرسوم البيانية --------
    # اتجاه الحضور لآخر 7 أيام
    attendance_trend = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        c = db.execute(
            "SELECT COUNT(*) c FROM attendance WHERE date=? AND status='حاضر'", (d,)
        ).fetchone()["c"]
        attendance_trend.append({"date": d[5:], "count": c})

    # توزيع حالة المعدات
    equipment_status = db.execute(
        "SELECT status, COUNT(*) c FROM equipment GROUP BY status"
    ).fetchall()

    # قيمة المخزون حسب الموقع
    stock_value = db.execute(
        """SELECT s.name as site_name, ROUND(SUM(ms.quantity * m.cost_per_unit), 0) as value
           FROM material_stock ms
           JOIN materials m ON m.id = ms.material_id
           JOIN sites s ON s.id = ms.site_id
           GROUP BY s.id ORDER BY s.km_from"""
    ).fetchall()

    # الميزانية مقابل المصروف لكل موقع
    budget_data = db.execute(
        "SELECT name, budget, spent, progress_pct FROM sites ORDER BY km_from"
    ).fetchall()

    total_budget = sum(s["budget"] or 0 for s in sites)
    total_spent = sum(s["spent"] or 0 for s in sites)

    db.close()
    return render_template(
        "dashboard.html",
        sites=sites,
        attendance_today=attendance_today,
        total_workers=total_workers,
        present_today=present_today,
        maintenance_due=maintenance_due,
        low_stock=low_stock,
        pending_transfers=pending_transfers,
        attendance_trend=attendance_trend,
        equipment_status=equipment_status,
        stock_value=stock_value,
        budget_data=budget_data,
        total_budget=total_budget,
        total_spent=total_spent,
        today=today,
    )


# ================================================================== المواقع / القواطع
@app.route("/sites", methods=["GET", "POST"])
@login_required
def sites():
    db = get_db()
    if request.method == "POST":
        if session.get("role") not in ("admin", "manager"):
            flash("ليست لديك صلاحية إضافة موقع", "error")
            db.close()
            return redirect(url_for("sites"))
        try:
            name = request.form["name"].strip()
            code = request.form["code"].strip()
            if not name or not code:
                raise ValueError("الاسم والرمز مطلوبان")
            db.execute(
                """INSERT INTO sites (name, code, km_from, km_to, budget, manager_name,
                                       latitude, longitude)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name, code,
                    request.form.get("km_from") or None,
                    request.form.get("km_to") or None,
                    request.form.get("budget") or 0,
                    request.form.get("manager_name", "").strip() or None,
                    request.form.get("latitude") or None,
                    request.form.get("longitude") or None,
                ),
            )
            log_activity(db, session.get("username"), f"إضافة موقع: {name} ({code})", "sites")
            db.commit()
            flash("تم إضافة الموقع بنجاح", "success")
        except Exception as e:
            db.rollback()
            flash(f"تعذّرت إضافة الموقع: رمز الموقع مستخدم مسبقًا أو بيانات غير صحيحة", "error")
        db.close()
        return redirect(url_for("sites"))

    q = request.args.get("q", "").strip()
    if q:
        all_sites = db.execute(
            "SELECT * FROM sites WHERE name LIKE ? OR code LIKE ? ORDER BY km_from",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        all_sites = db.execute("SELECT * FROM sites ORDER BY km_from").fetchall()
    db.close()

    sites_geo = [
        {
            "id": s["id"],
            "name": s["name"],
            "code": s["code"],
            "status": s["status"],
            "progress_pct": s["progress_pct"],
            "latitude": s["latitude"],
            "longitude": s["longitude"],
            "url": url_for("site_detail", site_id=s["id"]),
        }
        for s in all_sites if s["latitude"] is not None and s["longitude"] is not None
    ]
    return render_template("sites.html", sites=all_sites, sites_geo=sites_geo, q=q)


@app.route("/sites/<int:site_id>")
@login_required
def site_detail(site_id):
    db = get_db()
    site = db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        db.close()
        abort(404)

    workers = db.execute(
        "SELECT * FROM workers WHERE site_id=? AND active=1 ORDER BY name", (site_id,)
    ).fetchall()
    equipment = db.execute(
        """SELECT e.*, (e.operating_hours - e.hours_at_last_maint) as hours_since_maint
           FROM equipment e WHERE site_id=? ORDER BY name""",
        (site_id,),
    ).fetchall()
    stock = db.execute(
        """SELECT ms.*, m.name as material_name, m.unit, m.min_threshold
           FROM material_stock ms JOIN materials m ON m.id = ms.material_id
           WHERE ms.site_id=? ORDER BY m.name""",
        (site_id,),
    ).fetchall()
    today = date.today().isoformat()
    present_today = db.execute(
        "SELECT COUNT(*) c FROM attendance WHERE site_id=? AND date=? AND status='حاضر'",
        (site_id, today),
    ).fetchone()["c"]
    db.close()
    return render_template(
        "site_detail.html", site=site, workers=workers, equipment=equipment,
        stock=stock, present_today=present_today, today=today,
    )


@app.route("/sites/<int:site_id>/update", methods=["POST"])
@roles_required("admin", "manager")
def update_site(site_id):
    db = get_db()
    try:
        lat = request.form.get("latitude", "").strip()
        lng = request.form.get("longitude", "").strip()
        db.execute(
            """UPDATE sites SET progress_pct=?, budget=?, spent=?, status=?, manager_name=?,
                                 latitude=?, longitude=?
               WHERE id=?""",
            (
                float(request.form.get("progress_pct") or 0),
                float(request.form.get("budget") or 0),
                float(request.form.get("spent") or 0),
                request.form.get("status", "قيد التنفيذ"),
                request.form.get("manager_name", "").strip() or None,
                float(lat) if lat else None,
                float(lng) if lng else None,
                site_id,
            ),
        )
        log_activity(db, session.get("username"), f"تحديث بيانات الموقع رقم {site_id}", "sites")
        db.commit()
        flash("تم تحديث بيانات الموقع", "success")
    except Exception:
        db.rollback()
        flash("تعذّر تحديث بيانات الموقع", "error")
    db.close()
    return redirect(url_for("site_detail", site_id=site_id))


# ================================================================== العمالة
@app.route("/workers", methods=["GET", "POST"])
@login_required
def workers():
    db = get_db()
    if request.method == "POST":
        if session.get("role") not in ("admin", "manager"):
            flash("ليست لديك صلاحية إضافة عامل", "error")
            db.close()
            return redirect(url_for("workers"))
        try:
            name = request.form["name"].strip()
            if not name:
                raise ValueError("الاسم مطلوب")
            db.execute(
                """INSERT INTO workers (name, role, phone, national_id, daily_wage, site_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    request.form.get("role", "").strip(),
                    request.form.get("phone", "").strip(),
                    request.form.get("national_id", "").strip(),
                    request.form.get("daily_wage") or 0,
                    request.form.get("site_id") or None,
                ),
            )
            log_activity(db, session.get("username"), f"إضافة عامل: {name}", "workers")
            db.commit()
            flash("تم إضافة العامل بنجاح", "success")
        except Exception:
            db.rollback()
            flash("تعذّرت إضافة العامل، تحقق من البيانات المدخلة", "error")
        db.close()
        return redirect(url_for("workers"))

    today = date.today().isoformat()
    q = request.args.get("q", "").strip()
    site_filter = request.args.get("site_id", "")

    query = """SELECT w.*, s.name as site_name,
                      a.status as today_status
               FROM workers w
               LEFT JOIN sites s ON s.id = w.site_id
               LEFT JOIN attendance a ON a.worker_id = w.id AND a.date = ?
               WHERE w.active = 1"""
    params = [today]
    if q:
        query += " AND (w.name LIKE ? OR w.role LIKE ? OR w.phone LIKE ?)"
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if site_filter:
        query += " AND w.site_id = ?"
        params.append(site_filter)
    query += " ORDER BY s.km_from, w.name"

    all_workers = db.execute(query, params).fetchall()
    all_sites = db.execute("SELECT * FROM sites ORDER BY km_from").fetchall()
    db.close()
    return render_template(
        "workers.html", workers=all_workers, sites=all_sites, today=today,
        q=q, site_filter=site_filter,
    )


@app.route("/workers/attendance", methods=["POST"])
@roles_required("admin", "manager")
def mark_attendance():
    db = get_db()
    try:
        worker_id = request.form["worker_id"]
        site_id = request.form["site_id"]
        status = request.form["status"]
        today = date.today().isoformat()
        check_in = request.form.get("check_in_time") or None
        overtime = request.form.get("overtime_hours") or 0

        db.execute(
            """INSERT INTO attendance (worker_id, site_id, date, status, check_in_time, overtime_hours)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(worker_id, date) DO UPDATE SET
                 status=excluded.status, site_id=excluded.site_id,
                 check_in_time=excluded.check_in_time, overtime_hours=excluded.overtime_hours""",
            (worker_id, site_id, today, status, check_in, overtime),
        )
        log_activity(db, session.get("username"), f"تسجيل حضور للعامل رقم {worker_id}: {status}", "workers")
        db.commit()
        flash("تم تسجيل الحضور", "success")
    except Exception:
        db.rollback()
        flash("تعذّر تسجيل الحضور", "error")
    db.close()
    return redirect(url_for("workers"))


# ================================================================== المعدات والأسطول
@app.route("/equipment", methods=["GET", "POST"])
@login_required
def equipment():
    db = get_db()
    if request.method == "POST":
        if session.get("role") not in ("admin", "manager"):
            flash("ليست لديك صلاحية تسجيل معدة", "error")
            db.close()
            return redirect(url_for("equipment"))
        try:
            name = request.form["name"].strip()
            code = request.form["code"].strip()
            if not name or not code:
                raise ValueError("بيانات ناقصة")
            db.execute(
                """INSERT INTO equipment (name, type, code, site_id, operating_hours,
                                           maintenance_interval, hours_at_last_maint,
                                           last_maintenance_date, status, fuel_cost_per_liter)
                   VALUES (?, ?, ?, ?, 0, ?, 0, ?, 'نشطة', ?)""",
                (
                    name,
                    request.form.get("type", "").strip(),
                    code,
                    request.form.get("site_id") or None,
                    request.form.get("maintenance_interval") or 250,
                    date.today().isoformat(),
                    request.form.get("fuel_cost_per_liter") or 0,
                ),
            )
            log_activity(db, session.get("username"), f"تسجيل معدة: {name} ({code})", "equipment")
            db.commit()
            flash("تم تسجيل المعدة بنجاح", "success")
        except Exception:
            db.rollback()
            flash("تعذّر تسجيل المعدة، تحقق أن الرمز غير مستخدم مسبقًا", "error")
        db.close()
        return redirect(url_for("equipment"))

    q = request.args.get("q", "").strip()
    query = """SELECT e.*, s.name as site_name,
                      (e.operating_hours - e.hours_at_last_maint) as hours_since_maint
               FROM equipment e LEFT JOIN sites s ON s.id = e.site_id"""
    params = []
    if q:
        query += " WHERE e.name LIKE ? OR e.code LIKE ? OR e.type LIKE ?"
        params = [f"%{q}%", f"%{q}%", f"%{q}%"]
    query += " ORDER BY e.name"
    all_equipment = db.execute(query, params).fetchall()
    all_sites = db.execute("SELECT * FROM sites ORDER BY km_from").fetchall()

    # إجمالي تكلفة الوقود لكل معدة
    fuel_costs = {}
    for row in db.execute(
        """SELECT equipment_id, SUM(fuel_liters) as total_liters
           FROM equipment_logs GROUP BY equipment_id"""
    ).fetchall():
        fuel_costs[row["equipment_id"]] = row["total_liters"] or 0

    db.close()
    return render_template(
        "equipment.html", equipment=all_equipment, sites=all_sites, q=q,
        fuel_costs=fuel_costs,
    )


@app.route("/equipment/<int:eq_id>/log", methods=["POST"])
@roles_required("admin", "manager")
def log_equipment(eq_id):
    db = get_db()
    try:
        log_type = request.form["log_type"]
        hours_used = float(request.form.get("hours_used") or 0)
        fuel_liters = float(request.form.get("fuel_liters") or 0)
        note = request.form.get("note", "").strip()
        today = date.today().isoformat()

        db.execute(
            """INSERT INTO equipment_logs (equipment_id, date, log_type, hours_used, fuel_liters, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (eq_id, today, log_type, hours_used, fuel_liters, note),
        )

        if log_type == "تشغيل":
            db.execute(
                "UPDATE equipment SET operating_hours = operating_hours + ? WHERE id = ?",
                (hours_used, eq_id),
            )
        elif log_type == "صيانة":
            eq = db.execute("SELECT operating_hours FROM equipment WHERE id=?", (eq_id,)).fetchone()
            db.execute(
                """UPDATE equipment SET status='نشطة', last_maintenance_date=?,
                                         hours_at_last_maint=? WHERE id=?""",
                (today, eq["operating_hours"], eq_id),
            )
        log_activity(db, session.get("username"), f"تسجيل حركة ({log_type}) للمعدة رقم {eq_id}", "equipment")
        db.commit()
        flash("تم تسجيل الحركة", "success")
    except Exception:
        db.rollback()
        flash("تعذّر تسجيل الحركة", "error")
    db.close()
    return redirect(url_for("equipment"))


@app.route("/equipment/<int:eq_id>/status", methods=["POST"])
@roles_required("admin", "manager")
def update_equipment_status(eq_id):
    db = get_db()
    try:
        db.execute(
            "UPDATE equipment SET status=?, site_id=? WHERE id=?",
            (request.form["status"], request.form.get("site_id") or None, eq_id),
        )
        log_activity(db, session.get("username"), f"تحديث حالة المعدة رقم {eq_id}", "equipment")
        db.commit()
        flash("تم تحديث حالة المعدة", "success")
    except Exception:
        db.rollback()
        flash("تعذّر تحديث حالة المعدة", "error")
    db.close()
    return redirect(url_for("equipment"))


# ================================================================== المواد والمخزون
@app.route("/materials", methods=["GET", "POST"])
@login_required
def materials():
    db = get_db()
    if request.method == "POST":
        if session.get("role") not in ("admin", "manager"):
            flash("ليست لديك صلاحية إضافة مادة", "error")
            db.close()
            return redirect(url_for("materials"))
        try:
            name = request.form["name"].strip()
            unit = request.form["unit"].strip()
            if not name or not unit:
                raise ValueError("بيانات ناقصة")
            db.execute(
                "INSERT INTO materials (name, unit, min_threshold, cost_per_unit) VALUES (?, ?, ?, ?)",
                (
                    name, unit,
                    request.form.get("min_threshold") or 0,
                    request.form.get("cost_per_unit") or 0,
                ),
            )
            log_activity(db, session.get("username"), f"إضافة مادة: {name}", "materials")
            db.commit()
            flash("تمت إضافة المادة بنجاح", "success")
        except Exception:
            db.rollback()
            flash("تعذّرت إضافة المادة", "error")
        db.close()
        return redirect(url_for("materials"))

    all_materials = db.execute("SELECT * FROM materials ORDER BY name").fetchall()
    all_sites = db.execute("SELECT * FROM sites ORDER BY km_from").fetchall()

    q = request.args.get("q", "").strip()
    stock_query = """SELECT ms.*, m.name as material_name, m.unit, m.min_threshold,
                             s.name as site_name
                      FROM material_stock ms
                      JOIN materials m ON m.id = ms.material_id
                      JOIN sites s ON s.id = ms.site_id"""
    params = []
    if q:
        stock_query += " WHERE m.name LIKE ? OR s.name LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    stock_query += " ORDER BY m.name, s.km_from"
    stock_rows = db.execute(stock_query, params).fetchall()

    transfers = db.execute(
        """SELECT t.*, m.name as material_name, m.unit,
                  sf.name as from_name, st.name as to_name
           FROM material_transfers t
           JOIN materials m ON m.id = t.material_id
           LEFT JOIN sites sf ON sf.id = t.from_site_id
           JOIN sites st ON st.id = t.to_site_id
           ORDER BY t.date DESC, t.id DESC LIMIT 20"""
    ).fetchall()

    total_stock_value = db.execute(
        """SELECT ROUND(SUM(ms.quantity * m.cost_per_unit), 0) as v
           FROM material_stock ms JOIN materials m ON m.id = ms.material_id"""
    ).fetchone()["v"] or 0

    db.close()
    return render_template(
        "materials.html",
        materials=all_materials,
        sites=all_sites,
        stock_rows=stock_rows,
        transfers=transfers,
        q=q,
        total_stock_value=total_stock_value,
    )


@app.route("/materials/stock", methods=["POST"])
@roles_required("admin", "manager")
def update_stock():
    db = get_db()
    try:
        material_id = request.form["material_id"]
        site_id = request.form["site_id"]
        quantity = float(request.form["quantity"])
        if quantity < 0:
            raise ValueError("الكمية لا يمكن أن تكون سالبة")

        db.execute(
            """INSERT INTO material_stock (material_id, site_id, quantity)
               VALUES (?, ?, ?)
               ON CONFLICT(material_id, site_id) DO UPDATE SET quantity=?""",
            (material_id, site_id, quantity, quantity),
        )
        log_activity(db, session.get("username"), f"تحديث رصيد مادة رقم {material_id} في موقع رقم {site_id}", "materials")
        db.commit()
        flash("تم تحديث الرصيد", "success")
    except Exception:
        db.rollback()
        flash("تعذّر تحديث الرصيد، تحقق من القيمة المدخلة", "error")
    db.close()
    return redirect(url_for("materials"))


@app.route("/materials/transfer", methods=["POST"])
@roles_required("admin", "manager")
def request_transfer():
    db = get_db()
    try:
        quantity = float(request.form["quantity"])
        if quantity <= 0:
            raise ValueError("الكمية يجب أن تكون أكبر من صفر")
        db.execute(
            """INSERT INTO material_transfers (material_id, from_site_id, to_site_id,
                                                 quantity, date, status, note, requested_by)
               VALUES (?, ?, ?, ?, ?, 'قيد الانتظار', ?, ?)""",
            (
                request.form["material_id"],
                request.form.get("from_site_id") or None,
                request.form["to_site_id"],
                quantity,
                date.today().isoformat(),
                request.form.get("note", "").strip(),
                session.get("username"),
            ),
        )
        log_activity(db, session.get("username"), "طلب تحويل مادة جديد", "materials")
        db.commit()
        flash("تم إرسال طلب التحويل", "success")
    except Exception:
        db.rollback()
        flash("تعذّر إرسال طلب التحويل، تحقق من الكمية المدخلة", "error")
    db.close()
    return redirect(url_for("materials"))


@app.route("/materials/transfer/<int:transfer_id>/complete", methods=["POST"])
@roles_required("admin", "manager")
def complete_transfer(transfer_id):
    db = get_db()
    t = db.execute("SELECT * FROM material_transfers WHERE id=?", (transfer_id,)).fetchone()
    if t and t["status"] == "قيد الانتظار":
        if t["from_site_id"]:
            available = db.execute(
                "SELECT quantity FROM material_stock WHERE material_id=? AND site_id=?",
                (t["material_id"], t["from_site_id"]),
            ).fetchone()
            if not available or available["quantity"] < t["quantity"]:
                flash("لا يمكن تنفيذ التحويل: الرصيد غير كافٍ في الموقع المصدر", "error")
                db.close()
                return redirect(url_for("materials"))
            db.execute(
                """UPDATE material_stock SET quantity = quantity - ?
                   WHERE material_id=? AND site_id=?""",
                (t["quantity"], t["material_id"], t["from_site_id"]),
            )
        db.execute(
            """INSERT INTO material_stock (material_id, site_id, quantity)
               VALUES (?, ?, ?)
               ON CONFLICT(material_id, site_id) DO UPDATE SET quantity = quantity + ?""",
            (t["material_id"], t["to_site_id"], t["quantity"], t["quantity"]),
        )
        db.execute(
            "UPDATE material_transfers SET status='مكتمل' WHERE id=?", (transfer_id,)
        )
        log_activity(db, session.get("username"), f"تنفيذ طلب تحويل رقم {transfer_id}", "materials")
        db.commit()
        flash("تم تنفيذ التحويل وتحديث الأرصدة", "success")
    db.close()
    return redirect(url_for("materials"))


@app.route("/materials/transfer/<int:transfer_id>/reject", methods=["POST"])
@roles_required("admin", "manager")
def reject_transfer(transfer_id):
    db = get_db()
    t = db.execute("SELECT * FROM material_transfers WHERE id=?", (transfer_id,)).fetchone()
    if t and t["status"] == "قيد الانتظار":
        db.execute("UPDATE material_transfers SET status='مرفوض' WHERE id=?", (transfer_id,))
        log_activity(db, session.get("username"), f"رفض طلب تحويل رقم {transfer_id}", "materials")
        db.commit()
        flash("تم رفض طلب التحويل", "success")
    db.close()
    return redirect(url_for("materials"))


# ================================================================== التقارير والتصدير
@app.route("/reports")
@login_required
def reports():
    db = get_db()
    date_from = request.args.get("date_from") or (date.today() - timedelta(days=6)).isoformat()
    date_to = request.args.get("date_to") or date.today().isoformat()
    site_id = request.args.get("site_id", "")

    query = """SELECT w.name as worker_name, w.role, s.name as site_name,
                      a.date, a.status, a.check_in_time, a.overtime_hours
               FROM attendance a
               JOIN workers w ON w.id = a.worker_id
               JOIN sites s ON s.id = a.site_id
               WHERE a.date BETWEEN ? AND ?"""
    params = [date_from, date_to]
    if site_id:
        query += " AND a.site_id = ?"
        params.append(site_id)
    query += " ORDER BY a.date DESC, s.km_from"
    attendance_rows = db.execute(query, params).fetchall()

    summary = db.execute(
        """SELECT w.name as worker_name,
                  SUM(CASE WHEN a.status='حاضر' THEN 1 ELSE 0 END) as present_days,
                  SUM(CASE WHEN a.status='غائب' THEN 1 ELSE 0 END) as absent_days,
                  SUM(CASE WHEN a.status='إجازة' THEN 1 ELSE 0 END) as leave_days,
                  SUM(a.overtime_hours) as total_overtime,
                  w.daily_wage,
                  ROUND(SUM(CASE WHEN a.status='حاضر' THEN w.daily_wage ELSE 0 END), 2) as total_wage
           FROM attendance a
           JOIN workers w ON w.id = a.worker_id
           WHERE a.date BETWEEN ? AND ?
           GROUP BY w.id ORDER BY w.name""",
        (date_from, date_to),
    ).fetchall()

    all_sites = db.execute("SELECT * FROM sites ORDER BY km_from").fetchall()
    db.close()
    return render_template(
        "reports.html", attendance_rows=attendance_rows, summary=summary,
        sites=all_sites, date_from=date_from, date_to=date_to, site_id=site_id,
    )


def _build_workbook(headers, rows, sheet_title="تقرير"):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title[:31]
    ws.sheet_view.rightToLeft = True

    header_fill = PatternFill(start_color="17223F", end_color="17223F", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True, name="Calibri", size=12)

    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for r, row in enumerate(rows, start=2):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)

    for col in ws.columns:
        max_len = max((len(str(cell.value)) if cell.value is not None else 0) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 3, 12), 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.route("/export/<entity>")
@login_required
def export_entity(entity):
    db = get_db()
    today = date.today().isoformat()

    if entity == "workers":
        rows = db.execute(
            """SELECT w.name, w.role, w.phone, w.national_id, w.daily_wage,
                      s.name as site_name
               FROM workers w LEFT JOIN sites s ON s.id = w.site_id
               WHERE w.active=1 ORDER BY w.name"""
        ).fetchall()
        headers = ["الاسم", "الدور", "الهاتف", "الرقم الوطني", "الأجر اليومي", "الموقع"]
        data = [[r["name"], r["role"], r["phone"], r["national_id"], r["daily_wage"], r["site_name"]] for r in rows]
        filename = "قائمة_العمالة.xlsx"

    elif entity == "equipment":
        rows = db.execute(
            """SELECT e.name, e.type, e.code, e.status, s.name as site_name,
                      e.operating_hours, e.maintenance_interval,
                      (e.operating_hours - e.hours_at_last_maint) as hours_since_maint
               FROM equipment e LEFT JOIN sites s ON s.id = e.site_id ORDER BY e.name"""
        ).fetchall()
        headers = ["الاسم", "النوع", "الرمز", "الحالة", "الموقع", "ساعات التشغيل",
                   "دورة الصيانة", "ساعات منذ آخر صيانة"]
        data = [[r["name"], r["type"], r["code"], r["status"], r["site_name"],
                 r["operating_hours"], r["maintenance_interval"], round(r["hours_since_maint"] or 0, 1)] for r in rows]
        filename = "قائمة_المعدات.xlsx"

    elif entity == "materials":
        rows = db.execute(
            """SELECT m.name, m.unit, s.name as site_name, ms.quantity, m.min_threshold, m.cost_per_unit
               FROM material_stock ms
               JOIN materials m ON m.id = ms.material_id
               JOIN sites s ON s.id = ms.site_id
               ORDER BY m.name, s.km_from"""
        ).fetchall()
        headers = ["المادة", "الوحدة", "الموقع", "الرصيد", "الحد الأدنى", "التكلفة/الوحدة"]
        data = [[r["name"], r["unit"], r["site_name"], r["quantity"], r["min_threshold"], r["cost_per_unit"]] for r in rows]
        filename = "أرصدة_المواد.xlsx"

    elif entity == "sites":
        rows = db.execute("SELECT * FROM sites ORDER BY km_from").fetchall()
        headers = ["الاسم", "الرمز", "من كم", "إلى كم", "نسبة الإنجاز %", "الميزانية", "المصروف", "الحالة", "المدير"]
        data = [[r["name"], r["code"], r["km_from"], r["km_to"], r["progress_pct"],
                 r["budget"], r["spent"], r["status"], r["manager_name"]] for r in rows]
        filename = "المواقع_والقواطع.xlsx"

    elif entity == "attendance":
        date_from = request.args.get("date_from") or today
        date_to = request.args.get("date_to") or today
        rows = db.execute(
            """SELECT w.name as worker_name, s.name as site_name, a.date, a.status,
                      a.check_in_time, a.overtime_hours
               FROM attendance a
               JOIN workers w ON w.id = a.worker_id
               JOIN sites s ON s.id = a.site_id
               WHERE a.date BETWEEN ? AND ?
               ORDER BY a.date DESC""",
            (date_from, date_to),
        ).fetchall()
        headers = ["العامل", "الموقع", "التاريخ", "الحالة", "وقت الحضور", "ساعات إضافية"]
        data = [[r["worker_name"], r["site_name"], r["date"], r["status"], r["check_in_time"], r["overtime_hours"]] for r in rows]
        filename = "تقرير_الحضور.xlsx"

    else:
        db.close()
        abort(404)

    log_activity(db, session.get("username"), f"تصدير تقرير: {entity}", "reports")
    db.commit()
    db.close()

    buf = _build_workbook(headers, data, sheet_title=entity)
    return send_file(
        buf, as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ================================================================== سجل النشاط (للمدير فقط)
@app.route("/activity-log")
@roles_required("admin")
def activity_log_view():
    db = get_db()
    category = request.args.get("category", "")
    query = "SELECT * FROM activity_log"
    params = []
    if category:
        query += " WHERE category = ?"
        params.append(category)
    query += " ORDER BY id DESC LIMIT 200"
    logs = db.execute(query, params).fetchall()
    db.close()
    return render_template("activity_log.html", logs=logs, category=category)


# ================================================================== إدارة المستخدمين (للمدير فقط)
@app.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users():
    db = get_db()
    if request.method == "POST":
        try:
            username = request.form["username"].strip()
            full_name = request.form["full_name"].strip()
            password = request.form["password"]
            role = request.form.get("role", "viewer")
            if not username or not password or len(password) < 4:
                raise ValueError("بيانات غير صالحة")
            db.execute(
                "INSERT INTO users (username, password_hash, full_name, role) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), full_name, role),
            )
            log_activity(db, session.get("username"), f"إنشاء مستخدم جديد: {username}", "users")
            db.commit()
            flash("تم إنشاء المستخدم بنجاح", "success")
        except Exception:
            db.rollback()
            flash("تعذّر إنشاء المستخدم، تحقق أن اسم المستخدم غير مستخدم مسبقًا", "error")
        db.close()
        return redirect(url_for("users"))

    all_users = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    db.close()
    return render_template("users.html", users=all_users, role_labels=ROLE_LABELS)


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
@roles_required("admin")
def toggle_user(user_id):
    db = get_db()
    if user_id == session.get("user_id"):
        flash("لا يمكنك تعطيل حسابك الخاص", "error")
        db.close()
        return redirect(url_for("users"))
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if user:
        new_state = 0 if user["active"] else 1
        db.execute("UPDATE users SET active=? WHERE id=?", (new_state, user_id))
        log_activity(db, session.get("username"), f"{'تفعيل' if new_state else 'تعطيل'} مستخدم: {user['username']}", "users")
        db.commit()
        flash("تم تحديث حالة المستخدم", "success")
    db.close()
    return redirect(url_for("users"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
