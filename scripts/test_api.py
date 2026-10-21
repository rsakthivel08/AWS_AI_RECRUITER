import json
import boto3

lambda_client = boto3.client("lambda", region_name="ap-south-1")
FUNCTION_NAME = "ai-recruiter-dev-backend-apiC8550315-Hkm6ldLKdlAu"
SUB = "1143dd8a-c041-70b6-dad1-d03abc481b27"

def make_event(method: str, path: str, path_params=None, query_params=None, body=None):
    return {
        "rawPath": path,
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": "test-client"
            },
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": SUB,
                        "email": "test@example.com"
                    }
                }
            }
        },
        "pathParameters": path_params or {},
        "queryStringParameters": query_params or {},
        "headers": {
            "content-type": "application/json"
        },
        "body": json.dumps(body) if body else ""
    }

def test_endpoint(name: str, method: str, path: str, path_params=None, query_params=None, body=None):
    event = make_event(method, path, path_params, query_params, body)
    response = lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(event)
    )
    payload = json.loads(response["Payload"].read().decode("utf-8"))
    status_code = payload.get("statusCode")
    body_data = json.loads(payload.get("body", "{}")) if "body" in payload else payload
    print(f"\n[{name}] Status: {status_code}")
    if status_code == 200:
        if isinstance(body_data, dict):
            keys = list(body_data.keys())
            print(f"  Success! Keys returned: {keys}")
            for k in keys[:3]:
                val = body_data[k]
                val_summary = len(val) if isinstance(val, (list, dict)) else str(val)[:50]
                print(f"    {k}: {val_summary}")
        else:
            print(f"  Success! Body items: {len(body_data)}")
    else:
        print(f"  ERROR: {body_data}")

if __name__ == "__main__":
    test_endpoint("GET /candidates", "GET", "/candidates")
    test_endpoint("GET /jobs", "GET", "/jobs")
    test_endpoint("GET /candidates/c_187a1e2e4125", "GET", "/candidates/c_187a1e2e4125", path_params={"candidateId": "c_187a1e2e4125"})
