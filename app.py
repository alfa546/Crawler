from flask import Flask
from crawler.routes import crawler_bp
import logging
import os

env_file = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_file):
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                key, val = line.split('=', 1)
                os.environ[key] = val.strip("'\"")
def create_app():
    app = Flask(__name__, static_folder='static', template_folder='templates')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
    
    app.register_blueprint(crawler_bp)
    
    @app.errorhandler(Exception)
    def handle_exception(e):
        import traceback
        traceback.print_exc()
        return 'Server Error: ' + str(e), 500

    
    return app

app = create_app()

if __name__ == '__main__':
    # debug=True on 0.0.0.0 exposes the Werkzeug interactive debugger — an
    # unauthenticated remote-code-execution hole on any LAN. Enable it only
    # explicitly, via FLASK_DEBUG=1, and never on a public interface.
    debug = os.environ.get('FLASK_DEBUG', '') == '1'
    app.run(host='0.0.0.0', port=5002, threaded=True, debug=debug, use_reloader=False)
