import os
from flask import Flask, render_template

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Register blueprints
from application.routes.user_routes import user_blueprint
from application.routes.tender_api import tender_api
from application.routes.stock_analysis_api import stock_analysis_api
from application.routes.heatmap import heatmap_bp
from application.routes.ai_portfolio_api import ai_portfolio_api
from application.routes.stock_lookup_api import stock_lookup_api
from application.routes.advanced_analytics_api import advanced_analytics_api

app.register_blueprint(user_blueprint)
app.register_blueprint(tender_api)
app.register_blueprint(stock_analysis_api)
app.register_blueprint(heatmap_bp)
app.register_blueprint(ai_portfolio_api)
app.register_blueprint(stock_lookup_api)
app.register_blueprint(advanced_analytics_api)

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
    return {
        "current_user": {
            "name": session.get("name"),
            "email": session.get("email"),
            "id": session.get("user_id"),
        }
    }