"""本地安装入口。"""

from setuptools import find_packages, setup


setup(
    name="oil-supply-resilience",
    version="0.1.0",
    description="油气供应韧性、调度与现场巡检准入服务",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
