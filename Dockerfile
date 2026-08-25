FROM python:3.12-slim
WORKDIR /workspace
COPY . .
CMD ["python", "-m", "factory_controller.cli", "--help"]

