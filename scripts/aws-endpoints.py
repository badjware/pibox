"""Print the AWS endpoints that need SigV4 signing, as JSON.

Run by launch.py with the Python interpreter of the pibox image, which has the
AWS CLI and its botocore service models. Prints an object with:

    interpreter: real path of this interpreter, which runs aws and boto3
    endpoints:   list of {host, signing_service[, signing_region]}
"""

import glob
import gzip
import json
import os
import sys

import awscli  # noqa: F401  AWS CLI v2 bundles botocore and makes it importable
import botocore

endpoints = {}
for path in glob.glob(os.path.join(os.path.dirname(botocore.__file__), "data", "*", "*", "service-2.json*")):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        meta = json.load(f)["metadata"]
    auth = meta.get("auth", [])
    if meta.get("signatureVersion") not in ("v4", "s3v4") and not any(a.endswith("#sigv4") for a in auth):
        continue
    prefix = meta["endpointPrefix"]
    name = meta.get("signingName") or prefix
    # the proxy reads the region from regional host names
    endpoints[f"{prefix}.*.amazonaws.com"] = (name, None)
    if meta.get("globalEndpoint"):
        endpoints[meta["globalEndpoint"]] = (name, "us-east-1")

# Virtual-hosted S3 buckets. The legacy global form (**.s3.amazonaws.com) is
# left out: OpenShell rejects it next to this one because their signing
# regions differ and the wildcards may overlap.
endpoints["**.s3.*.amazonaws.com"] = ("s3", None)

print(json.dumps({
    "interpreter": os.path.realpath(sys.executable),
    "endpoints": [{"host": host, "signing_service": name, **({"signing_region": region} if region else {})}
                  for host, (name, region) in sorted(endpoints.items())],
}))
