from setuptools import setup, find_packages

setup(
    name="synergy_to_ewm",
    version="1.0.0",
    description="Migrate IBM Rational Synergy to IBM Engineering Workflow Management (EWM)",
    packages=find_packages(),
    package_data={"synergy_to_ewm": ["mapping_default.yaml"]},
    python_requires=">=3.9",
    install_requires=[
        "requests>=2.31.0",
        "urllib3>=2.0.0",
        "PyYAML>=6.0",
    ],
    entry_points={
        "console_scripts": [
            "synergy-to-ewm=synergy_to_ewm.migrate:main",
        ]
    },
)
