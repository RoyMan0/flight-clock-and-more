from flask import Blueprint, render_template

pages_bp = Blueprint("pages", __name__)


@pages_bp.get("/")
def dashboard():
    return render_template("dashboard.html")


@pages_bp.get("/config")
def config_page():
    return render_template("config.html")


@pages_bp.get("/flights")
def flights_page():
    return render_template("flights.html")


@pages_bp.get("/display")
def display_page():
    return render_template("display.html")
