import sys
import os

app_dir = os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from dotenv import load_dotenv
load_dotenv(os.path.join(app_dir, '.env'))

from app import app as application
