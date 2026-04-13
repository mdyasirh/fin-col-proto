#!/bin/bash
echo "Setting up FitLife Time Tracker..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py makemigrations tracker
python manage.py migrate
python manage.py seed
echo "Starting server..."
python manage.py runserver
