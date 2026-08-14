import requests
import json

BASE_URL = "https://app.americansocceranalysis.com/api/v1"


def test_asa_nwsl():
    url = f"{BASE_URL}/nwsl/games/xgoals"

    print(f"Requesting: {url}")

    response = requests.get(url, timeout=30)

    print("Status:", response.status_code)
    response.raise_for_status()

    data = response.json()

    print("\nTop-level Python type:")
    print(type(data))

    print("\nFirst portion of response:")
    print(json.dumps(data, indent=2)[:5000])


if __name__ == "__main__":
    test_asa_nwsl()
