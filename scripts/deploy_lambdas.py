"""Deploy code and env vars to AWS Lambda functions."""
import json
import os
import time
import zipfile
from pathlib import Path
import boto3

REGION = "ap-south-1"
BASE_DIR = Path(__file__).resolve().parents[1]
ZIP_PATH = BASE_DIR / "lambda_api_patch.zip"

COMMON_ENV = {
    "TABLE_NAME": "recruiter-dev",
    "UPLOADS_BUCKET": "ar-dev-uploads",
    "TEXT_BUCKET": "ar-dev-uploads",
    "EXPORTS_BUCKET": "ar-dev-exports",
    "VECTOR_BUCKET": "ar-dev-vectors",
    "VECTOR_INDEX": "resume-vectors",
    "BEDROCK_MODEL_JUDGE": "amazon.nova-pro-v1",
    "BEDROCK_MODEL_SMALL": "amazon.nova-micro-v1",
    "BEDROCK_MODEL_VISION": "amazon.nova-lite-v1",
    "BEDROCK_MODEL_EMBED": "amazon.titan-embed-text-v2:0",
    "EMBED_DIMENSIONS": "3072",
    "SSM_PATH": "/ai-recruiter/dev/",
    "ENV": "dev",
    "PROMPT_VERSION": "v1",
    "DAILY_MATCH_LIMIT": "500",
    "AI_PROVIDER": "groq",
    "GROQ_API_KEY": "gsk_REDACTED-push-to-github",
    "GEMINI_API_KEY": "AQ.Ab8REDACTED-push-to-github",
    "GROQ_MODEL_TEXT": "openai/gpt-oss-20b",
    "GEMINI_MODEL_TEXT": "gemini-2.5-flash-lite",
    "GEMINI_MODEL_EMBED": "gemini-embedding-001",
    "AWS_POWERTOOLS_DISABLED": "1",
}

FUNCTION_EXTRAS = {
    "ai-recruiter-dev-backend-apiC8550315-Hkm6ldLKdlAu": {
        "MATCH_MACHINE_ARN": "arn:aws:states:ap-south-1:942893240068:stateMachine:ar-dev-match",
        "JOB_MACHINE_ARN": "arn:aws:states:ap-south-1:942893240068:stateMachine:ar-dev-job",
    },
    "ai-recruiter-dev-backend-ingestTriggerEE3DFA6B-zs9ROpPCfM2s": {
        "INGEST_MACHINE_ARN": "arn:aws:states:ap-south-1:942893240068:stateMachine:ar-dev-ingest",
        "JOB_MACHINE_ARN": "arn:aws:states:ap-south-1:942893240068:stateMachine:ar-dev-job",
    },
}

TARGET_FUNCTIONS = [
    "ai-recruiter-dev-backend-apiC8550315-Hkm6ldLKdlAu",
    "ai-recruiter-dev-backend-parseJd9EE5AA46-wS8hhLNugPDW",
    "ai-recruiter-dev-backend-scoreCandidate3E6DF976-HenzNSVfbNtr",
    "ai-recruiter-dev-backend-extractProfile0A57264A-lc3erDoEFA6Q",
    "ai-recruiter-dev-backend-finalizeRankingD58F5EB5-U0Iv21GF4sDw",
    "ai-recruiter-dev-backend-validateDedup15E07D80-NMFeYTKyCihE",
    "ai-recruiter-dev-backend-extractText4BFE7A68-R5eji3p9uqP4",
    "ai-recruiter-dev-backend-ingestTriggerEE3DFA6B-zs9ROpPCfM2s",
]

def build_zip():
    print(f"Building {ZIP_PATH}...")
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for folder in ["src", "prompts", "data"]:
            folder_path = BASE_DIR / folder
            if not folder_path.exists():
                continue
            for root, dirs, files in os.walk(folder_path):
                dirs[:] = [d for d in dirs if d != "__pycache__" and not d.endswith(".egg-info")]
                for file in files:
                    if file.endswith((".pyc", ".pyo")):
                        continue
                    full_path = Path(root) / file
                    arcname = full_path.relative_to(BASE_DIR)
                    zf.write(full_path, arcname)
    print(f"Zip created. Size: {ZIP_PATH.stat().st_size / (1024*1024):.2f} MB")

def deploy():
    build_zip()
    lambda_client = boto3.client("lambda", region_name=REGION)
    zip_bytes = ZIP_PATH.read_bytes()

    for fn_name in TARGET_FUNCTIONS:
        print(f"\n--- Deploying {fn_name} ---")
        env_vars = dict(COMMON_ENV)
        if fn_name in FUNCTION_EXTRAS:
            env_vars.update(FUNCTION_EXTRAS[fn_name])

        # 1. Update Environment Variables
        print("  Updating configuration (env vars)...")
        try:
            lambda_client.update_function_configuration(
                FunctionName=fn_name,
                Environment={"Variables": env_vars}
            )
            waiter = lambda_client.get_waiter("function_updated")
            waiter.wait(FunctionName=fn_name)
            print("  Configuration updated.")
        except Exception as e:
            print(f"  Failed to update config: {e}")

        # 2. Update Code
        print("  Updating function code...")
        try:
            lambda_client.update_function_code(
                FunctionName=fn_name,
                ZipFile=zip_bytes
            )
            waiter = lambda_client.get_waiter("function_updated")
            waiter.wait(FunctionName=fn_name)
            print("  Code updated.")
        except Exception as e:
            print(f"  Failed to update code: {e}")

    print("\nAll target functions updated successfully!")

if __name__ == "__main__":
    deploy()
