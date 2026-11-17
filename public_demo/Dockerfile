FROM daobrain-app:dev
COPY public_demo /app/public_demo
COPY tests/test_public_demo.py /app/tests/test_public_demo.py
CMD ["uvicorn", "public_demo.main:app", "--host", "0.0.0.0", "--port", "18100", "--no-access-log"]
