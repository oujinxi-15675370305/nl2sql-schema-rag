.PHONY: seed run test eval clean docker-build docker-up docker-down shots

seed:
	python scripts/seed_demo_db.py

run:
	python main.py

test:
	python -m unittest discover -s tests -v

eval:
	python -m app.evals

docker-build:
	docker compose build

docker-up:
	docker compose up --build

docker-down:
	docker compose down

shots:
	python scripts/shots.py

clean:
	rm -rf data/demo.sqlite __pycache__ app/__pycache__ tests/__pycache__
