import os
from flask import Flask, render_template, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32).hex()

# Treat the app as "production" only when FLASK_DEBUG is explicitly off.
# Anything else (FLASK_DEBUG=1, unset locally, or app.run(debug=True)) keeps
# Flask's normal dev behaviour so templates and static files reload on edit.
_FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "").lower()
_PROD = _FLASK_DEBUG in ("0", "false", "no")
if _PROD:
    app.config["TEMPLATES_AUTO_RELOAD"] = False
    # Long-cache /static/* in production; browsers + CDN revalidate via ETag.
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 30
else:
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ── Shared cache (Redis when configured, in-process fallback otherwise) ──
from application.services.cache import cache, mark_active as _cache_mark_active
if cache is not None:
    cache.init_app(app)

# ── Performance middleware: gzip + Cache-Control/ETag + request timing ──
from application.services import perf as _perf
_perf.install(app)


# Track active users so the background precomputer knows whom to refresh.
@app.before_request
def _track_active_user():
    uid = session.get("user_id")
    if uid:
        try:
            _cache_mark_active(uid)
        except Exception:
            pass


# Register blueprints
from application.routes.user_routes import user_blueprint
from application.routes.tender_api import tender_api
from application.routes.stock_analysis_api import stock_analysis_api
from application.routes.heatmap import heatmap_bp
from application.routes.ai_portfolio_api import ai_portfolio_api
from application.routes.stock_lookup_api import stock_lookup_api
from application.routes.advanced_analytics_api import advanced_analytics_api
from application.routes.intraday_api import intraday_api
from application.routes.volume_api import volume_api
from application.routes.broker_api import broker_api
from application.routes.billing import billing_bp
from application.routes.rag_admin import rag_admin_bp
from application.routes.fundamentals_api import fundamentals_api
from application.routes.option_chain_api import option_chain_api
from application.routes.market_pulse_api import market_pulse_api
from application.routes.global_markets_api import global_markets_api
from application.routes.intraday_tools_api import intraday_tools_api
from application.routes.swing_tools_api import swing_tools_api
from application.routes.investing_tools_api import investing_tools_api
from application.routes.ai_tools_api import ai_tools_api
from application.routes.mutual_funds_api import mutual_funds_api
from application.routes.health_api import health_api

app.register_blueprint(user_blueprint)
app.register_blueprint(tender_api)
app.register_blueprint(stock_analysis_api)
app.register_blueprint(heatmap_bp)
app.register_blueprint(ai_portfolio_api)
app.register_blueprint(stock_lookup_api)
app.register_blueprint(advanced_analytics_api)
app.register_blueprint(intraday_api)
app.register_blueprint(volume_api)
app.register_blueprint(broker_api)
app.register_blueprint(billing_bp)
app.register_blueprint(rag_admin_bp)
app.register_blueprint(fundamentals_api)
app.register_blueprint(option_chain_api)
app.register_blueprint(market_pulse_api)
app.register_blueprint(global_markets_api)
app.register_blueprint(intraday_tools_api)
app.register_blueprint(swing_tools_api)
app.register_blueprint(investing_tools_api)
app.register_blueprint(ai_tools_api)
app.register_blueprint(mutual_funds_api)
app.register_blueprint(health_api)

# Plain routes registered against the app
from application.routes import route  # noqa: F401, E402


@app.errorhandler(404)
def _not_found(_e):
    return render_template("error.html", code=404,
                           message="The page you are looking for does not exist."), 404


@app.errorhandler(500)
def _server_error(_e):
    return render_template("error.html", code=500,
                           message="Something went wrong. Please try again."), 500


@app.context_processor
def _inject_globals():
    from flask import session
    from application.services.market_data import provider_name
    from application.services import plans
    from application.constants import PERSONAS, get_persona, persona_groups

    plan_dict = None
    if session.get("email"):
        try:
            plan_dict = plans.current_plan()
        except Exception:
            plan_dict = plans.PLANS["free"]

    _plan_id = (plan_dict or {}).get("id", "free")
    _rank = {"free": 0, "pro": 1, "elite": 2}.get(_plan_id, 0)

    _persona_id = session.get("persona")

    return {
        "current_user": {
            "name": session.get("name"),
            "email": session.get("email"),
            "id": session.get("user_id"),
        },
        "market_data_provider": provider_name(),
        "current_plan": plan_dict,
        "has_pro": _rank >= 1,
        "has_elite": _rank >= 2,
        "user_persona": get_persona(_persona_id),
        "persona_groups": persona_groups(_persona_id),
        "all_personas": PERSONAS,
    }


# ── Daily RAG ingestion scheduler (free, in-process) ──
# Pulls news + filings for every symbol any user holds, stores them in
# Azure Tables (RagDocs / RagEmbed). Disabled by RAG_ENABLE_SCHEDULER=0.
def _start_rag_scheduler():
    import os
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        # Avoid double-start under Flask debug reloader (parent process)
        return
    try:
        from application.config import RAG_ENABLE_SCHEDULER, RAG_INGEST_HOUR_IST
        if not RAG_ENABLE_SCHEDULER:
            return
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from application.services.rag.ingest import runner as rag_runner
        from application.services import cache as _cache

        def _run_if_leader():
            # Only the worker holding the Redis leader lock runs the job.
            if _cache.try_become_leader(ttl=3600):
                rag_runner.run_daily()

        sched = BackgroundScheduler(timezone="Asia/Kolkata", daemon=True)
        sched.add_job(
            _run_if_leader,
            trigger=CronTrigger(hour=RAG_INGEST_HOUR_IST, minute=0),
            id="rag_daily_ingest",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        sched.start()
        print(f"[rag] daily ingest scheduled @ {RAG_INGEST_HOUR_IST:02d}:00 IST")
    except Exception as e:
        print(f"[rag] scheduler start failed: {e}")


_start_rag_scheduler()


# ── Background precompute (market + per-user payloads → Redis) ────────
def _start_precompute_scheduler():
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    try:
        from application.services import precompute
        precompute.start_scheduler()
    except Exception as e:
        print(f"[precompute] scheduler start failed: {e}")


_start_precompute_scheduler()
