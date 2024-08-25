# Reddit Stash

Reddit Stash is a Python script that automatically saves your Reddit saved posts and comments to Dropbox. It uses GitHub Actions to run the script on a daily schedule.

## Features
- Automatically retrieves saved posts and comments from Reddit.
- Saves the content as markdown files.
- Uploads the files to Dropbox for secure storage.

## Setup

### Prerequisites
- Python 3.10
- A Dropbox account with an API token.
- Reddit API credentials.

### Installation

1. Clone this repository:
   ```
   git clone https://github.com/rhnfzl/reddit-stash.git
   cd reddit-stash
   ```

2. Install the required Python packages:
    ```
    pip install -r requirements.txt
    ```

3. Set these environment variables in your OS before running the script, create a Reddit app on https://old.reddit.com/prefs/apps/ and then put the client ID, secret key, as well as your reddit username and password.

MacOS and Linux:
    ```
    export REDDIT_CLIENT_ID='your_client_id'
    export REDDIT_CLIENT_SECRET='your_client_secret'
    export REDDIT_USERNAME='your_username'
    export REDDIT_PASSWORD='your_password'
    ```
Windows:

    ```
    set REDDIT_CLIENT_ID='your_client_id'
    set REDDIT_CLIENT_SECRET='your_client_secret'
    set REDDIT_USERNAME='your_username'
    set REDDIT_PASSWORD='your_password'
    ```
You can check the config has been setup properly or not below:

    ```
    echo $REDDIT_CLIENT_ID
    echo $REDDIT_CLIENT_SECRET
    echo $REDDIT_USERNAME
    echo $REDDIT_PASSWORD
    ```
4. Usage Run the script manually:
    ```
    python reddit_stash.py
    ```

    or using the following Automation
This project uses GitHub Actions to automatically run the script daily at midnight CET and upload the files to Dropbox. The workflow is defined in .github/workflows/reddit_scraper.yml.

### Contributing
Feel free to open issues or submit pull requests if you have any improvements or bug fixes.