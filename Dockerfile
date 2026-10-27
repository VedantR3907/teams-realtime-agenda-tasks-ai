# Use an official Python runtime as a base image
FROM python:3.12.9-slim

# Set the working directory
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt /app/

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . /app/

# Expose the port FastAPI will run on
EXPOSE 8000

# Use Gunicorn to run the app
CMD ["python", "uvicorn_config.py"]