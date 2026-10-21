$Region = "ap-south-1"

Write-Host "1. Building lambda_api_patch.zip..."
python scripts/build_zip.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to build zip"
    exit 1
}

$functions = @(
    @{ Name = "ai-recruiter-dev-backend-apiC8550315-Hkm6ldLKdlAu"; Env = "scripts/env_api.json" },
    @{ Name = "ai-recruiter-dev-backend-ingestTriggerEE3DFA6B-zs9ROpPCfM2s"; Env = "scripts/env_trigger.json" },
    @{ Name = "ai-recruiter-dev-backend-parseJd9EE5AA46-wS8hhLNugPDW"; Env = "scripts/env_common.json" },
    @{ Name = "ai-recruiter-dev-backend-scoreCandidate3E6DF976-HenzNSVfbNtr"; Env = "scripts/env_common.json" },
    @{ Name = "ai-recruiter-dev-backend-extractProfile0A57264A-lc3erDoEFA6Q"; Env = "scripts/env_common.json" },
    @{ Name = "ai-recruiter-dev-backend-finalizeRankingD58F5EB5-U0Iv21GF4sDw"; Env = "scripts/env_common.json" },
    @{ Name = "ai-recruiter-dev-backend-validateDedup15E07D80-NMFeYTKyCihE"; Env = "scripts/env_common.json" },
    @{ Name = "ai-recruiter-dev-backend-extractText4BFE7A68-R5eji3p9uqP4"; Env = "scripts/env_common.json" }
)

foreach ($fn in $functions) {
    $name = $fn.Name
    $envFile = $fn.Env
    Write-Host "`n=== Updating $name ==="
    
    Write-Host "  Waiting for function to be ready..."
    aws lambda wait function-updated --function-name $name --region $Region
    
    Write-Host "  Updating configuration from $envFile..."
    aws lambda update-function-configuration --function-name $name --environment "file://$envFile" --region $Region | Out-Null
    
    Write-Host "  Waiting for configuration update..."
    aws lambda wait function-updated --function-name $name --region $Region
    
    Write-Host "  Updating code from lambda_api_patch.zip..."
    aws lambda update-function-code --function-name $name --zip-file fileb://lambda_api_patch.zip --region $Region | Out-Null
    
    Write-Host "  Waiting for code update..."
    aws lambda wait function-updated --function-name $name --region $Region
    
    Write-Host "  Done updating $name"
}

Write-Host "`nAll Lambdas successfully updated!"
