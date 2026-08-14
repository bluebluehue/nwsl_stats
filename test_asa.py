import requests

BASE_URL = "https://app.americansocceranalysis.com/api/v1"


def main():
    url = f"{BASE_URL}/openapi.json"

    print(f"Requesting API specification: {url}")

    response = requests.get(url, timeout=30)
    print("Status:", response.status_code)
    response.raise_for_status()

    spec = response.json()

    paths = spec.get("paths", {})

    nwsl_paths = sorted(
        path for path in paths.keys()
        if "/nwsl/" in path.lower()
    )

    print("\n=== AVAILABLE NWSL API ENDPOINTS ===")

    for path in nwsl_paths:
        methods = paths[path]

        method_names = [
            method.upper()
            for method in methods.keys()
            if method.lower() in ["get", "post", "put", "delete", "patch"]
        ]

        print(f"{', '.join(method_names):8} {path}")

        for method_name, method_info in methods.items():
            if method_name.lower() != "get":
                continue

            summary = method_info.get("summary")
            description = method_info.get("description")

            if summary:
                print(f"         Summary: {summary}")

            if description:
                clean_description = " ".join(description.split())
                print(f"         Description: {clean_description[:250]}")

            parameters = method_info.get("parameters", [])

            if parameters:
                print("         Parameters:")

                for param in parameters:
                    name = param.get("name")
                    required = param.get("required", False)

                    schema = param.get("schema", {})
                    param_type = schema.get("type", "")

                    print(
                        f"           - {name}"
                        f"{' [required]' if required else ''}"
                        f"{f' ({param_type})' if param_type else ''}"
                    )

        print()


if __name__ == "__main__":
    main()
