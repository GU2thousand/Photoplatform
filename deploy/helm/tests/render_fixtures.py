"""Render validation fixtures for kubeconform; no image or AWS claim is made."""
import argparse
from pathlib import Path
import subprocess

from test_chart import ROOT, fixture, render


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--kubeconform", help="Path to pinned kubeconform, if API schema validation is requested")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, values in [
        ("dev", fixture()), ("prod", fixture("prod")),
        ("local", fixture(local=True)), ("full", fixture("prod", all_features=True)),
    ]:
        path = args.output_dir / f"{name}.yaml"
        result = render(values, output_path=path)
        if result.returncode:
            raise SystemExit(f"{name}: {result.stderr}")
        if args.kubeconform:
            for version in ["1.34.0", "1.36.0"]:
                # ASCP CRD is checked by the chart tests/provider shape; Kubernetes
                # published schemas cover the real built-in resources below.
                subprocess.run([args.kubeconform, "-strict", "-summary", "-kubernetes-version", version,
                                "-skip", "SecretProviderClass", str(path)], check=True, cwd=ROOT, timeout=120)
    print("Rendered 4 synthetic configuration fixtures (dev, prod, local, full).")


if __name__ == "__main__":
    main()
