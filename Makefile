.PHONY: seed run test eval clean

seed:
	python scripts/seed_demo_db.py

run:
	python main.py

test:
	python -m unittest discover -s tests -v

eval:
	python -m app.evals

clean:
	rm -rf data/demo.sqlite __pycache__ app/__pycache__ tests/__pycache__
