"""Admin web UI for the multi-exchange auto-buy bot.

Lets VAs register clients (name, email, license #, exchange, sub-account,
ticker) in a SQLite registry, and applies the enabled entries to the bot's
config.json. API secrets never pass through this app or its database —
applied entries reference environment variables (${SUB1_API_KEY} style)
whose values live only in the server's .env file.

Meant to be reached over Tailscale only; never publish its port to the
public internet. An optional shared password can be set via WEB_PASSWORD.

Run:  python -m webapp.app  (or the `webapp` service in docker-compose.legacy.yml)
"""

import csv
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
from datetime import datetime, timezone

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)

EXCHANGES = ["Bitget", "Bybit", "OKX", "Binance"]

# Venues whose API keys carry a passphrase; the rest need only key + secret.
NEEDS_PASSPHRASE = {"Bitget", "OKX"}

# Basket weight bounds. Kept in step with autobuy.WEIGHT_MIN/WEIGHT_MAX (this
# app must never accept a weight the bot would refuse to load); duplicated
# rather than imported so the UI stays runnable without the bot module.
WEIGHT_MIN, WEIGHT_MAX = 1e-9, 1e9

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    license_number TEXT NOT NULL DEFAULT '',
    exchange TEXT NOT NULL DEFAULT 'Bitget',
    subaccount TEXT NOT NULL,
    ticker TEXT NOT NULL,
    min_usdt REAL NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

FIELDS = ("name", "email", "license_number", "exchange", "subaccount", "ticker")


def parse_ticker_field(value):
    """Parse the ticker box into [(COIN, weight), ...].

    "XLM"              -> one coin, the original behaviour
    "XRP,XLM,ADA"      -> a basket, split evenly
    "XRP:3, XLM:2"     -> a basket, weighted 60/40

    Raises ValueError with a message meant for the user's screen.
    """
    legs, seen = [], set()
    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        coin, _, weight = part.partition(":")
        coin = coin.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{1,20}", coin):
            raise ValueError("ticker must be plain symbols like BTC or ETH, "
                             "optionally several separated by commas")
        if coin in seen:
            raise ValueError(f"{coin} is listed twice")
        seen.add(coin)
        if weight.strip():
            try:
                w = float(weight)
            except ValueError:
                raise ValueError(f"weight for {coin} is not a number")
            # Mirrors autobuy.WEIGHT_MIN/MAX. Anything the box accepts has to
            # load in the bot — "inf"/"nan"/1e400 would parse fine here and
            # then make the bot skip the whole client without telling anyone.
            if not WEIGHT_MIN <= w <= WEIGHT_MAX:
                raise ValueError(f"weight for {coin} must be a number between "
                                 f"{WEIGHT_MIN:g} and {WEIGHT_MAX:g}")
        else:
            w = 1.0
        legs.append((coin, w))
    if not legs:
        raise ValueError("ticker is required")
    return legs


def env_prefix(subaccount):
    """Sub-account name -> env var prefix: 'sub 1-btc' -> 'SUB_1_BTC'."""
    return re.sub(r"[^A-Za-z0-9]+", "_", subaccount).strip("_").upper()


def key_vars(subaccount, exchange="Bitget"):
    p = env_prefix(subaccount)
    names = [f"{p}_API_KEY", f"{p}_API_SECRET"]
    if exchange in NEEDS_PASSPHRASE:
        names.append(f"{p}_API_PASSPHRASE")
    return names


def keys_present(subaccount, exchange="Bitget"):
    return all(os.environ.get(v) for v in key_vars(subaccount, exchange))


def create_app(data_dir=None):
    app = Flask(__name__)
    app.config["DATA_DIR"] = data_dir or os.environ.get("DATA_DIR", "data")
    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    app.config["DB_PATH"] = os.path.join(app.config["DATA_DIR"], "registry.db")
    app.config["BOT_CONFIG"] = os.environ.get(
        "AUTOBUY_CONFIG", os.path.join(app.config["DATA_DIR"], "config.json"))

    # Persistent session secret so logins survive restarts.
    secret_path = os.path.join(app.config["DATA_DIR"], ".flask-secret")
    if os.environ.get("SECRET_KEY"):
        app.secret_key = os.environ["SECRET_KEY"]
    else:
        if not os.path.exists(secret_path):
            with open(secret_path, "w", encoding="utf-8") as fh:
                fh.write(secrets.token_hex(32))
            os.chmod(secret_path, 0o600)
        with open(secret_path, "r", encoding="utf-8") as fh:
            app.secret_key = fh.read().strip()

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB_PATH"])
            g.db.row_factory = sqlite3.Row
            g.db.executescript(SCHEMA)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------- auth

    @app.before_request
    def require_login():
        password = os.environ.get("WEB_PASSWORD")
        if not password:
            return None
        if request.endpoint in ("login", "static"):
            return None
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if secrets.compare_digest(request.form.get("password", ""),
                                      os.environ.get("WEB_PASSWORD", "")):
                session["authed"] = True
                return redirect(request.args.get("next") or url_for("index"))
            error = "Wrong password."
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("index"))

    # ------------------------------------------------------------ pages

    @app.route("/")
    def index():
        rows = db().execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()
        clients = [dict(r) for r in rows]
        for c in clients:
            c["keys_ok"] = keys_present(c["subaccount"], c["exchange"])
            c["env_vars"] = key_vars(c["subaccount"], c["exchange"])
        applied_at = None
        if os.path.exists(app.config["BOT_CONFIG"]):
            applied_at = datetime.fromtimestamp(
                os.path.getmtime(app.config["BOT_CONFIG"]), tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
        return render_template("list.html", clients=clients, applied_at=applied_at,
                               locked=bool(os.environ.get("WEB_PASSWORD")))

    def _validate(form):
        data = {f: form.get(f, "").strip() for f in FIELDS}
        data["notes"] = form.get("notes", "").strip()
        data["min_usdt"] = form.get("min_usdt", "5").strip() or "5"
        data["enabled"] = 1 if form.get("enabled") else 0
        errors = []
        for f in ("name", "email", "subaccount", "ticker"):
            if not data[f]:
                errors.append(f"{f.replace('_', ' ')} is required")
        if data["email"] and "@" not in data["email"]:
            errors.append("email does not look valid")
        if data["exchange"] not in EXCHANGES:
            errors.append("unknown exchange")
        if data["ticker"]:
            try:
                parse_ticker_field(data["ticker"])
            except ValueError as exc:
                errors.append(str(exc))
        try:
            data["min_usdt"] = float(data["min_usdt"])
            if data["min_usdt"] < 0:
                raise ValueError
        except ValueError:
            errors.append("minimum USDT must be a non-negative number")
        # Canonical form: "xrp:3, xlm" -> "XRP:3,XLM" (weights kept, 1s dropped)
        if data["ticker"] and not errors:
            data["ticker"] = ",".join(
                c if w == 1.0 else "%s:%g" % (c, w)
                for c, w in parse_ticker_field(data["ticker"]))
        else:
            data["ticker"] = data["ticker"].upper()
        return data, errors

    @app.route("/add", methods=["GET", "POST"])
    def add():
        if request.method == "POST":
            data, errors = _validate(request.form)
            if not errors:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                db().execute(
                    "INSERT INTO clients (name, email, license_number, exchange, subaccount,"
                    " ticker, min_usdt, enabled, notes, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (data["name"], data["email"], data["license_number"], data["exchange"],
                     data["subaccount"], data["ticker"], data["min_usdt"], data["enabled"],
                     data["notes"], now, now))
                db().commit()
                flash(f"Added {data['name']} — auto-buys {data['ticker']} in sub-account "
                      f"“{data['subaccount']}”. Remember to press “Apply to bot”.")
                return redirect(url_for("index"))
            for e in errors:
                flash(e, "error")
        return render_template("form.html", client=request.form if request.method == "POST" else {},
                               exchanges=EXCHANGES, title="Add client")

    @app.route("/edit/<int:client_id>", methods=["GET", "POST"])
    def edit(client_id):
        row = db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if row is None:
            abort(404)
        if request.method == "POST":
            data, errors = _validate(request.form)
            if not errors:
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                db().execute(
                    "UPDATE clients SET name=?, email=?, license_number=?, exchange=?,"
                    " subaccount=?, ticker=?, min_usdt=?, enabled=?, notes=?, updated_at=?"
                    " WHERE id=?",
                    (data["name"], data["email"], data["license_number"], data["exchange"],
                     data["subaccount"], data["ticker"], data["min_usdt"], data["enabled"],
                     data["notes"], now, client_id))
                db().commit()
                flash(f"Updated {data['name']}. Remember to press “Apply to bot”.")
                return redirect(url_for("index"))
            for e in errors:
                flash(e, "error")
            return render_template("form.html", client=request.form, exchanges=EXCHANGES,
                                   title="Edit client")
        return render_template("form.html", client=dict(row), exchanges=EXCHANGES,
                               title="Edit client")

    @app.route("/delete/<int:client_id>", methods=["POST"])
    def delete(client_id):
        db().execute("DELETE FROM clients WHERE id = ?", (client_id,))
        db().commit()
        flash("Deleted. Remember to press “Apply to bot”.")
        return redirect(url_for("index"))

    # ----------------------------------------------------------- export

    @app.route("/export.csv")
    def export_csv():
        rows = db().execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE").fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Name", "Email", "License #", "Exchange", "Sub Account",
                         "Ticker", "Min USDT", "Enabled", "Notes", "Created", "Updated"])
        for r in rows:
            writer.writerow([r["name"], r["email"], r["license_number"], r["exchange"],
                             r["subaccount"], r["ticker"], r["min_usdt"],
                             "yes" if r["enabled"] else "no", r["notes"],
                             r["created_at"], r["updated_at"]])
        return send_file(io.BytesIO(buf.getvalue().encode("utf-8-sig")),
                         mimetype="text/csv", as_attachment=True,
                         download_name="clients.csv")

    @app.route("/apply", methods=["POST"])
    def apply():
        """Write all enabled registry entries into the bot's config.json.

        Secrets are written as ${VAR} references only; the bot skips entries
        whose vars aren't in .env yet and picks up the new file automatically.
        """
        rows = db().execute("SELECT * FROM clients WHERE enabled = 1"
                            " ORDER BY name COLLATE NOCASE").fetchall()
        cfg_path = app.config["BOT_CONFIG"]
        existing = {}
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
            except (OSError, json.JSONDecodeError):
                existing = {}

        subaccounts, missing = [], []
        for r in rows:
            kv = key_vars(r["subaccount"], r["exchange"])
            entry = {
                "name": r["subaccount"],
                "exchange": r["exchange"].lower(),
                "api_key": "${%s}" % kv[0],
                "api_secret": "${%s}" % kv[1],
                "min_usdt_to_trigger": r["min_usdt"],
                "enabled": True,
            }
            legs = parse_ticker_field(r["ticker"])
            if len(legs) == 1:
                entry["buy_coin"] = legs[0][0]
            else:
                # A basket splits each arriving deposit across its coins by
                # weight; the bot reads this instead of buy_coin.
                entry["basket"] = [{"coin": c, "weight": w} for c, w in legs]
            if len(kv) == 3:  # passphrase venues (Bitget, OKX)
                entry["api_passphrase"] = "${%s}" % kv[2]
            subaccounts.append(entry)
            if not keys_present(r["subaccount"], r["exchange"]):
                missing.append((r["subaccount"], [v for v in kv if not os.environ.get(v)]))

        cfg = {"poll_seconds": existing.get("poll_seconds", 30), "subaccounts": subaccounts}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cfg_path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, cfg_path)

        if not subaccounts:
            flash("Applied: 0 enabled clients — the bot is now idle.", "error")
        else:
            flash(f"Applied {len(subaccounts)} client(s) to the bot config. "
                  "The bot picks this up within one poll cycle — no restart needed.")
        for sub, vars_missing in missing:
            flash(f"“{sub}” has no API keys yet — the bot will skip it until "
                  f"{', '.join(vars_missing)} are added to the server's .env file.", "error")
        return redirect(url_for("index"))

    return app


if __name__ == "__main__":
    create_app().run(host=os.environ.get("BIND", "127.0.0.1"),
                     port=int(os.environ.get("PORT", "8080")))
