cwlVersion: v1.2
$graph:
- class: Workflow
  label: disasters-iam-probe
  doc: "TEMPORARY IAM diagnostic. Prints the DPS worker's AWS identity (role ARN +\
    \ account), then attempts ONE Secrets Manager read and reports only OK or the\
    \ error code/message \u2014 never the secret value. Reads, writes and publishes\
    \ no data; exits 0 even on denial. Used to determine whether the DPS worker role\
    \ can read a cross-account Secrets Manager secret BEFORE building the KMS setup:\
    \ the secret need not exist, because AWS distinguishes an identity-policy denial\
    \ (\"no identity-based policy allows the secretsmanager:GetSecretValue action\"\
    ) from a missing resource (ResourceNotFoundException)."
  id: disasters-iam-probe
  inputs:
    secret_arn:
      doc: "Full ARN of a us-west-2 Secrets Manager secret to attempt reading. The\
        \ secret need NOT exist \u2014 a made-up ARN in your own account still separates\
        \ \"no identity-based policy allows secretsmanager:GetSecretValue\" (the worker\
        \ role cannot read secrets at all; only MAAP can change that) from ResourceNotFoundException\
        \ (the action is permitted; proceed to create the customer-managed KMS key,\
        \ the secret, and its resource policy). Defaulted to a placeholder ARN so\
        \ a bare Submit answers the question. Blank = print the worker identity only."
      label: Secret ARN to probe
      type: string?
      default: arn:aws:secretsmanager:us-west-2:515966502221:secret:disasters/dps/probe-AAAAAA
  outputs:
    output:
      type: Directory
      outputSource: process/outputs_result
  steps:
    process:
      run: '#main'
      in:
        secret_arn: secret_arn
      out:
      - outputs_result
- class: CommandLineTool
  id: main
  requirements:
    DockerRequirement:
      dockerPull: ghcr.io/disasters-learning-portal/disasters-product-algorithms:deploy-algorithm
    NetworkAccess:
      networkAccess: true
    ResourceRequirement:
      ramMin: 2
      coresMin: 1
      outdirMax: 1
  baseCommand: /app/disasters-product-algorithms/dps/probe/run.sh
  inputs:
    secret_arn:
      type: string?
      inputBinding:
        position: 1
        prefix: --secret_arn
      default: arn:aws:secretsmanager:us-west-2:515966502221:secret:disasters/dps/probe-AAAAAA
  outputs:
    outputs_result:
      outputBinding:
        glob: ./output*
      type: Directory
s:author:
- class: s:Person
  s:name: NASA Disasters
s:contributor:
- class: s:Person
  s:name: NASA Disasters
s:citation: NASA Disasters Program
s:codeRepository: https://github.com/Disasters-Learning-Portal/disasters-product-algorithms.git
s:commitHash: 6d495afc282865bb78a1f532b87c98cee8959b2f
s:dateCreated: 2026-08-12
s:license: Apache-2.0
s:softwareVersion: 1.0.0
s:version: dev
s:releaseNotes: Temporary IAM/Secrets Manager reachability probe for the DPS worker
  role; image built in-workflow from dps/Dockerfile.
s:keywords: diagnostic, iam, secrets-manager, temporary
$namespaces:
  s: https://schema.org/
$schemas:
- https://raw.githubusercontent.com/schemaorg/schemaorg/refs/heads/main/data/releases/9.0/schemaorg-current-http.rdf
