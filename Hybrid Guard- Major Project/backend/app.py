from flask import Flask, request, jsonify
import os
import pandas as pd
from flask_cors import CORS
from ml.model_loader import load_model
from ml.review_processing import detect_fake_reviews
from utils.file_handler import save_file, process_csv
from utils.web_scraper import scrape_reviews

# Initialize Flask App
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "http://localhost:5173"}})

# Set Upload Folder for CSV files
UPLOAD_FOLDER = "uploads"
PROCESSED_FOLDER = "processed_files"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["PROCESSED_FOLDER"] = PROCESSED_FOLDER

# Load ML Model & Vectorizer
model, vectorizer = load_model()


@app.route("/", methods=["GET"])
def home():
    return jsonify({"message": "Fake Product Detection API is running!"})


def save_processed_file(df, filename):
    """Saves the processed CSV with fake review marks."""
    processed_filepath = os.path.join(PROCESSED_FOLDER, f"analyzed_{filename}")
    df.to_csv(processed_filepath, index=False)
    return processed_filepath


@app.route("/upload", methods=["POST"])
def upload_file():
    """Handles CSV file uploads, processes reviews, and detects fake reviews."""
    if not model or not vectorizer:
        return jsonify({"error": "ML model not loaded. Check server logs."}), 500

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    filepath = save_file(file, app.config["UPLOAD_FOLDER"])
    if not filepath:
        return jsonify({"error": "Invalid file format. Please upload a CSV file."}), 400

    df, error_response, status_code = process_csv(filepath)
    if error_response:
        return error_response, status_code

    result = detect_fake_reviews(df, model, vectorizer)
    return jsonify({"message": "File processed successfully", **result}), 200


@app.route("/analyze", methods=["POST"])
def analyze_product():
    """Scrapes reviews from a URL and detects fake reviews."""
    if not model or not vectorizer:
        return jsonify({"error": "ML model not loaded. Check server logs."}), 500

    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"error": "No URL provided"}), 400

    url = data["url"].strip()
    if not url:
        return jsonify({"error": "URL cannot be empty"}), 400

    # Scrape reviews
    prod_id, product_name, csv_path, df = scrape_reviews(url)

    # ── FIX: scrape_reviews() returns an error STRING (not None) on failure ──
    # The old code did `if df is None or df.empty` which crashed with:
    #   AttributeError: 'str' object has no attribute 'empty'
    if not isinstance(df, pd.DataFrame):
        error_detail = df if isinstance(df, str) else "Unknown scraping error."
        return jsonify({"error": f"Scraping failed: {error_detail}"}), 500

    if df.empty:
        return jsonify({
            "error": (
                "No reviews were scraped. Flipkart may have updated its layout. "
                "Check scraped_files/page1_empty.png for a debug snapshot."
            )
        }), 500

    result = detect_fake_reviews(df, model, vectorizer)
    return jsonify({"message": "URL processed successfully", **result}), 200


if __name__ == "__main__":
    app.run(debug=True)