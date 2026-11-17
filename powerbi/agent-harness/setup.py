from setuptools import setup, find_namespace_packages

setup(
    name="cli-anything-powerbi",
    version="1.0.0",
    description="CLI harness for Power BI: PBIP/TMDL project editing and the Power BI REST API",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    package_data={"cli_anything.powerbi": ["skills/*.md", "README.md", "tests/TEST.md"]},
    install_requires=["click>=8.0.0", "prompt-toolkit>=3.0.0", "requests>=2.28", "msal>=1.24", "openpyxl>=3.0"],
    extras_require={"test": ["pytest>=7"]},
    entry_points={"console_scripts": ["cli-anything-powerbi=cli_anything.powerbi.powerbi_cli:main"]},
    python_requires=">=3.10",
)
