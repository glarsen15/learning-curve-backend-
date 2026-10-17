
import os
import re
from datetime import date

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
BACKEND_API_KEY = os.environ.get("BACKEND_API_KEY", "")

SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept": "application/json",
}

TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{}.json"

COST_TAGS = [
    "CostOfGoodsAndServicesSold",
    "CostOfRevenue",
    "CostOfGoodsSold",
    "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
    "OperatingExpenses",
]

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "Revenues",
]
OPERATING_INCOME_TAGS = [
    "OperatingIncomeLoss",
]


def sec_get(url):
    if not SEC_USER_AGENT or "@" not in SEC_USER_AGENT:
        raise RuntimeError(
            "The SEC_USER_AGENT environment variable must contain "
            "your name and contact email."
        )

    response = requests.get(url, headers=SEC_HEADERS, timeout=30)

    if response.status_code != 200:
        raise RuntimeError(
            f"SEC returned HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    return response.json()


def find_company(ticker):
    companies = sec_get(TICKER_URL)
    ticker = ticker.upper().strip()

    for company in companies.values():
        if company["ticker"].upper() == ticker:
            return {
                "ticker": ticker,
                "name": company["title"],
                "cik": str(company["cik_str"]).zfill(10),
            }

    raise ValueError(f"Ticker not found: {ticker}")


def annual_facts(facts, tags):
    """Find annual USD facts reported in 10-K filings."""
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    chosen = {}

    for tag in tags:
        node = us_gaap.get(tag, {})
        units = node.get("units", {}).get("USD", [])

        for item in units:
            if item.get("form") != "10-K":
                continue
            if item.get("fp") != "FY":
                continue
            if not item.get("start") or not item.get("end"):
                continue

            start = date.fromisoformat(item["start"])
            end = date.fromisoformat(item["end"])
            days = (end - start).days

            if not 350 <= days <= 380:
                continue

            period_end = item["end"]
            candidate = {
                "period_end": period_end,
                "value": item["val"],
                "tag": tag,
                "filed": item.get("filed"),
                "accession": item.get("accn"),
                "form": item.get("form"),
            }

            # Keep the first available tag for each fiscal year.
            # If that tag has multiple filings, keep the latest filing.
            if period_end not in chosen:
                chosen[period_end] = candidate
            elif chosen[period_end]["tag"] == tag:
                if (candidate["filed"] or "") > (
                    chosen[period_end]["filed"] or ""
                ):
                    chosen[period_end] = candidate

    return chosen


@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "message": "Learning Curve SEC backend is running."
    })


@app.get("/api/company")
def company_data():
    # Protect the financial-data endpoint with an API key.
    if (
        not BACKEND_API_KEY
        or request.headers.get("X-API-Key") != BACKEND_API_KEY
    ):
        return jsonify({"error": "Unauthorized"}), 401

    ticker = request.args.get("ticker", "").strip().upper()

    if not re.fullmatch(r"[A-Z0-9.-]{1,10}", ticker):
        return jsonify({"error": "Enter a valid ticker."}), 400

    try:
        company = find_company(ticker)
        facts = sec_get(FACTS_URL.format(company["cik"]))

        costs = annual_facts(facts, COST_TAGS)
        revenues = annual_facts(facts, REVENUE_TAGS)
        operating_income = annual_facts(
            facts, OPERATING_INCOME_TAGS
        )

        years = []

        # Include years for which we have revenue data.
        for period_end in sorted(revenues):
            revenue = revenues[period_end]
            cost = costs.get(period_end)
            op_income = operating_income.get(period_end)

            # Use reported costs when available.
            # Otherwise calculate revenue minus operating income.
            if cost:
                cost_value = cost["value"]
                cost_tag = cost["tag"]
                filing = cost
            elif op_income:
                cost_value = revenue["value"] - op_income["value"]
                cost_tag = "Calculated: revenue - operating income"
                filing = revenue
            else:
                continue

            years.append({
                "period": period_end[:4],
                "period_end": period_end,
                "cogs_usd": cost_value,
                "cogs_tag": cost_tag,
                "revenue_usd": revenue["value"],
                "revenue_tag": revenue["tag"],
                "filing_form": filing["form"],
                "filing_date": filing["filed"],
                "accession": filing["accession"],
            })

        return jsonify({
            "company": company,
            "source": "SEC EDGAR Company Facts API",
            "years": years[-12:],
        })

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("SEC retrieval failed")
        return jsonify({"error": str(exc)}), 502


    # Use reported costs when available.
    # Otherwise, calculate operating expenses from
    # revenue minus operating income.
    if cost:
        cost_value = cost["value"]
        cost_tag = cost["tag"]
        filing = cost
    elif op_income:
        cost_value = revenue["value"] - op_income["value"]
        cost_tag = "Calculated: revenue - operating income"
        filing = revenue
    else:
        continue

    years.append({
        "period": period_end[:4],
        "period_end": period_end,
        "cogs_usd": cost_value,
        "cogs_tag": cost_tag,
        "revenue_usd": revenue["value"],
        "revenue_tag": revenue["tag"],
        "filing_form": filing["form"],
        "filing_date": filing["filed"],
        "accession": filing["accession"],
    })

        return jsonify({
            "company": company,
            "source": "SEC EDGAR Company Facts API",
            "years": years[-12:],
        })

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("SEC retrieval failed")
        return jsonify({"error": str(exc)}), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
