# NWSL stats
A collection of nwsl stats for fantasy football collected from [Fantasy NWSL](https://www.fantasynwsl.com/)

## Process
The ```get_data.py``` script will query the GraphQL endpoint on Aerial to retrieve player data which includes contributions in completed matches, it will also query Aerial to retrieve fixtures for all teams. The script will then combine the player data with the future fixtures for that players club. Once complete the results are output into the ```transformed_data.json``` file. The data retrieved from the API is mapped to a more data table friendly format whilst being processed.

### Testing
You can run the scripts locally to generate the json files and run a local webserver to see the results:

Install dependencies with uv - ```uv sync```

Run the data script - ```uv run get_data.py```

Run the webserver - ```uv run python -m http.server 8000``` and see the resultant webpage on [http://[::]:8000/](http://[::]:8000/)
