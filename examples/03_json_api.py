"""JSON APIs: GET and POST JSON payloads.

Run:
    uv run python examples/03_json_api.py
"""

from scraper import Scraper


def main() -> None:
    s = Scraper()

    # get_json() sets an Accept: application/json header and parses the body.
    data = s.get_json("https://httpbin.org/json")
    print("GET json keys:", list(data.keys()))

    # post_json() sends a JSON body and parses the JSON response.
    echo = s.post_json(
        "https://httpbin.org/post",
        json={"title": "Hello", "chapters": [1, 2, 3]},
    )
    print("POST echoed json:", echo.get("json"))

    # Raw Response is available too when you need status/headers.
    resp = s.get("https://httpbin.org/get")
    print("status:", resp.status_code, "| content-type:", resp.headers.get("Content-Type"))


if __name__ == "__main__":
    main()
