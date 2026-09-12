from flask import Flask
from crawler.routes import crawler_bp
import logging

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
    app.run(host='0.0.0.0', port=5002, threaded=True, debug=True, use_reloader=False)
