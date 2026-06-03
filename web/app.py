#!/usr/bin/python3
"""
Flask web application factory.
Called from main.py with injected cfg, display, and plugin_manager references.
"""

import json
import os
from flask import Flask, render_template, jsonify, send_from_directory

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.abspath(os.path.join(WEB_DIR, ".."))

CLOSEST_FILE  = os.path.join(BASE_DIR, "close.txt")
FARTHEST_FILE = os.path.join(BASE_DIR, "farthest.txt")


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def create_app(cfg=None, display=None, plugin_manager=None, setup_mode=False):
    app = Flask(
        __name__,
        template_folder=os.path.join(WEB_DIR, "templates"),
        static_folder=os.path.join(WEB_DIR, "static"),
    )

    # Store references for blueprint access
    app.cfg = cfg
    app.display = display
    app.plugin_manager = plugin_manager

    # ------------------------------------------------------------------
    # Register blueprints
    # ------------------------------------------------------------------
    if setup_mode:
        from web.blueprints.setup import setup_bp
        app.register_blueprint(setup_bp)
    else:
        from web.blueprints.api import api_bp
        from web.blueprints.pages import pages_bp
        app.register_blueprint(api_bp, url_prefix="/api")
        app.register_blueprint(pages_bp)

    # ------------------------------------------------------------------
    # Legacy routes (preserved)
    # ------------------------------------------------------------------
    @app.get("/closest/json")
    def closest_json():
        return jsonify(_load_json(CLOSEST_FILE, {}))

    @app.get("/farthest/json")
    def farthest_json():
        return jsonify(_load_json(FARTHEST_FILE, []))

    @app.get("/closest")
    def closest_page():
        return render_template("closest_map.html")

    @app.get("/farthest")
    def farthest_page():
        return render_template("farthest_map.html")

    @app.get("/maps/<path:filename>")
    def maps(filename):
        maps_dir = os.path.join(WEB_DIR, "static", "maps")
        return send_from_directory(maps_dir, filename)

    return app


# Standalone run (legacy compatibility)
if __name__ == "__main__":
    standalone_app = create_app()
    standalone_app.run(host="0.0.0.0", port=8080, debug=False)
