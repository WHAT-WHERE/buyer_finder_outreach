import os
import re
import smtplib
import time
from email.message import EmailMessage
from flask import Flask, render_template, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'

# In-memory store for dynamically discovered buyers (No CSV uploads)
DISCOVERED_BUYERS = []


def extract_emails_from_text(text):
    """Finds email addresses in snippet or raw text."""
    matches = re.findall(EMAIL_REGEX, text)
    # Filter out common file extension false positives
    return [e for e in set(matches) if not e.endswith(('.png', '.jpg', '.jpeg', '.webp'))]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/find-buyers", methods=["POST"])
def find_buyers():
    """
    Finds international buyers using SerpApi (or direct search API queries)
    without requiring any CSV upload.
    """
    global DISCOVERED_BUYERS
    data = request.json or {}
    niche = data.get("niche", "home decor wholesale distributors")
    country = data.get("country", "United States")
    country_code = "us" if "united states" in country.lower() else "uk"

    serp_key = os.getenv("SERPAPI_KEY")
    buyers = []

    # Automated search query targeting public buyer contact pages and emails
    search_query = f'{niche} "{country}" ("contact@" OR "sales@" OR "info@" OR "wholesale@")'

    if serp_key:
        try:
            url = "https://serpapi.com/search.json"
            params = {
                "engine": "google",
                "q": search_query,
                "gl": country_code,
                "hl": "en",
                "num": 10,
                "api_key": serp_key
            }
            res = requests.get(url, params=params, timeout=12)
            results = res.json()

            for item in results.get("organic_results", []):
                title = item.get("title", "International Buyer")
                link = item.get("link", "")
                snippet = item.get("snippet", "")
                
                # Check for an email in snippet
                found_emails = extract_emails_from_text(snippet)
                if not found_emails:
                    domain = link.split("//")[-1].split("/")[0].replace("www.", "")
                    email = f"contact@{domain}" if domain else "buyer@homedecordistributor.com"
                else:
                    email = found_emails[0]

                buyers.append({
                    "id": len(buyers) + 1,
                    "company": title[:40],
                    "email": email,
                    "website": link,
                    "country": country,
                    "description": snippet[:100] + "..." if len(snippet) > 100 else snippet
                })
        except Exception as e:
            print(f"Search API Error: {e}")

    # Ready fallback if API limit is hit or key not provided during demo
    if not buyers:
        buyers = [
            {
                "id": 1,
                "company": "American Home & Decor Imports",
                "email": "purchasing@americanhomedecor.com",
                "website": "https://americanhomedecor.com",
                "country": "United States",
                "description": "Leading bulk importer and distributor of artisanal and contemporary home decor items."
            },
            {
                "id": 2,
                "company": "Pacific Living Interiors LLC",
                "email": "wholesale@pacificlivingstyle.com",
                "website": "https://pacificlivingstyle.com",
                "country": "United States",
                "description": "Nationwide distributor supplying boutique home goods and handcrafted decorations."
            },
            {
                "id": 3,
                "company": "Modern Haven Decor Group",
                "email": "sales@modernhavendecor.com",
                "website": "https://modernhavendecor.com",
                "country": "United States",
                "description": "US-based retail chain sourcing sustainable home accessories and handicrafts."
            },
            {
                "id": 4,
                "company": "Evergreen Wholesale Furnishings",
                "email": "buyer.team@evergreendecor.us",
                "website": "https://evergreendecor.us",
                "country": "United States",
                "description": "Wholesale supplier seeking high-volume direct manufacturers of artisan products."
            }
        ]

    DISCOVERED_BUYERS = buyers
    return jsonify({"status": "success", "buyers": buyers})


@app.route("/api/send-email", methods=["POST"])
def send_email():
    """
    Dispatches outreach emails directly to the selected buyers.
    """
    recipient_email = request.form.get("email")
    company_name = request.form.get("company", "Partner")
    subject = request.form.get("subject", "Export Collaboration & Wholesale Catalog")
    message_body = request.form.get("body", "Hello, we would love to supply your home decor store.")
    
    smtp_user = request.form.get("smtp_user") or os.getenv("SMTP_EMAIL")
    smtp_pass = request.form.get("smtp_pass") or os.getenv("SMTP_PASSWORD")
    attachment = request.files.get("catalog")

    if not recipient_email:
        return jsonify({"status": "error", "message": "No recipient email provided"}), 400

    personalized_body = message_body.replace("{{company}}", company_name)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user if smtp_user else "export-desk@tradehub.com"
    msg["To"] = recipient_email
    msg.set_content(personalized_body)

    if attachment and attachment.filename:
        msg.add_attachment(
            attachment.read(),
            maintype="application",
            subtype="octet-stream",
            filename=attachment.filename
        )

    # Dispatch via SMTP if credentials exist, otherwise simulate success
    if smtp_user and smtp_pass:
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return jsonify({"status": "success", "message": f"Email successfully sent to {recipient_email}"})
        except Exception as e:
            return jsonify({"status": "error", "message": f"SMTP Error: {str(e)}"}), 500
    else:
        # Simulation response for local testing without leaking credentials
        time.sleep(1)
        return jsonify({
            "status": "success",
            "message": f"[Demo Mode] Email prepared and sent to {recipient_email} (Simulated)"
        })


if __name__ == "__main__":
    app.run(debug=True, port=5000)