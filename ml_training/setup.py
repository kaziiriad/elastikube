"""Minimal setup.py so the `utils` package can be pip-installed.

The CronJob container installs `utils/` as a package so the scripts
can `from utils.feature_engineer import FeatureEngineer`.
"""
from setuptools import setup, find_packages

setup(
    name="k3s-autoscaler-ml",
    version="0.1.0",
    packages=find_packages(exclude=["tests", "notebooks", "docs"]),
    python_requires=">=3.11",
    install_requires=[
        "boto3>=1.34.0,<2.0.0",
        "pandas>=2.0.0,<3.0.0",
        "numpy>=1.24.0,<3.0.0",
        "prophet>=1.1.4,<2.0.0",
        "joblib>=1.3.0,<2.0.0",
    ],
)
