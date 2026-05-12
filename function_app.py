import azure.functions as func
import azure.durable_functions as df

from handlers.blob_trigger import bp as blob_bp
from handlers.tier1 import bp as tier1_bp
from handlers.tier2 import bp as tier2_bp
from handlers.tier3 import bp as tier3_bp

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

app.register_blueprint(blob_bp)
app.register_blueprint(tier1_bp)
app.register_blueprint(tier2_bp)
app.register_blueprint(tier3_bp)
