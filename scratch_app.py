from flask import Flask
from crawler.routes import crawler_bp
import logging

def create_app():
    app = Flask(__name__, static_folder='static', template_folder='templates')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
    
    app.register_blueprint(crawler_bp)
    
    return app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, threaded=True)
