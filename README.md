# Reddit Stash

Reddit Stash is a Python script that automatically saves your Reddit saved posts and comments to local or your Dropbox. For later it uses GitHub Actions to run the script on a daily schedule.

## Features
- Downloads the saved reddit folder from the dropbox.
- Automatically retrieves saved posts and comments from Reddit.
- Uploads the files to Dropbox for storage.
- Saves the content as markdown files.

## Setup

### Prerequisites
- Python 3.10
- A Dropbox account with an API token.
- Reddit API credentials.

### Installation

Set the reddit environment variables before following any installation flow below, here is [how to](#setting-up-reddit-environment-variables). Basically create a Reddit app on https://old.reddit.com/prefs/apps/ and then put the client ID, secret key, as well as your reddit username and password.


##### Github Action Installation

Note : In the following process you need [Dropbox App setup](#setting-up-dropbox-app), it automatically run the script daily at midnight CET and upload the files to Dropbox. The workflow is defined in .github/workflows/reddit_scraper.yml.

1. First fork this repository.

2. Set Up Secrets: Go to your forked repository’s Settings > Secrets and variables > Actions > Click on New repository secret
    Add the following as the secret `Name` individually:
    - `REDDIT_CLIENT_ID`
    - `REDDIT_CLIENT_SECRET`
    - `REDDIT_USERNAME`
    - `DROPBOX_TOKEN` (for Dropbox)
    and enter the repective `Secret` without any quotes.

It will look like this after adding all of them : ![Repositiory Secrets](resources/repositiory_secrets.png).

3. You can run it manually to test if it works or not clicking on the `Actions` tab > Select the `Reddit Stash Workflow` from the list on the left > Click `Run workflow` button > Select the branch `main` > Click the green `Run workflow` button. The workflow will then be triggered, and you can monitor its progress in the Actions tab. At the end of successful run you will see the reddit folder in your dropbox.


##### Local Installation

1. Clone this repository:
   ```
   git clone https://github.com/rhnfzl/reddit-stash.git
   cd reddit-stash
   ```

2. Install the required Python packages:
    ```
    pip install -r requirements.txt
    ```

3. Setup the [Dropbox App setup](#setting-up-dropbox-app). Skip it if you don't want to setup the dropbox and only want to save the file locally in your system.

4. 

MacOS and Linux:
    ```
    export REDDIT_CLIENT_ID='your_client_id'
    export REDDIT_CLIENT_SECRET='your_client_secret'
    export REDDIT_USERNAME='your_username'
    export REDDIT_PASSWORD='your_password'
    export DROPBOX_TOKEN='dropbox-token'
    ```

Windows:

    ```
    set REDDIT_CLIENT_ID='your_client_id'
    set REDDIT_CLIENT_SECRET='your_client_secret'
    set REDDIT_USERNAME='your_username'
    set REDDIT_PASSWORD='your_password'
    set DROPBOX_TOKEN='dropbox-token'
    ```
You can check the config has been setup properly or not below:
    ```
    echo $REDDIT_CLIENT_ID
    echo $REDDIT_CLIENT_SECRET
    echo $REDDIT_USERNAME
    echo $REDDIT_PASSWORD
    echo $DROPBOX_TOKEN
    ```

4. Usage Run the script:

    a. For the first time run the following code from the command line:
    ```
    python reddit_stash.py
    ```
    and if you would like to upload it to the Dropbox run:
    ```
    python dropbox_utils.py --upload
    ```
    b. From the next time onwards you can run the following code in this sequence:
    
    ```
    python dropbox_utils.py --download
    ```
    ```
    python reddit_stash.py
    ```
    ```
    python dropbox_utils.py --upload
    ```

###### Setting Up Reddit Environment Variables
    - Create a Reddit app on https://old.reddit.com/prefs/apps/
    - Setup the `name`, selct the script and provide the `redirect_uri` based on [PRAW docs](https://praw.readthedocs.io/en/latest/getting_started/authentication.html#password-flow).
    ![Step 1](resources/reddit_create_app1.png)
    - Copy the provided `REDDIT_CLIENT_ID` and the `REDDIT_CLIENT_SECRET` based on the following screenshot:
    ![Step 2](resources/reddit_create_app2.png)
    - `REDDIT_USERNAME` is your reddit username
    - `REDDIT_PASSWORD` is your reddit passowrd
    - Keep aside the credentials for the further setup.

###### Setting Up Dropbox app
    - Go to [Dropbox Developer App](https://www.dropbox.com/developers/apps).
    - Click on Create app.
    - Select `Scoped access` for the type of app.
    - Under `Choose the type of access you need`, select `Full Dropbox`.
    - Give your app a unique name, and click `Create app`.
    ![](resources/dropbox_app1.png)
    - Go to `Permissions` tab and check the following under `Files and folders`:
        - `files.metadata.write`
        - `files.metadata.read`
        - `files.content.write`
        - `files.content.read`
        - Click `Submit` in the bottom.
    ![](resources/dropbox_app2.png)
    - Go to `Settings` tab scroll down and click on `Generated access token`, this is your `DROPBOX_TOKEN`.
    - For more information about the setup visit [OAuth Guide](https://developers.dropbox.com/oauth-guide).

### Contributing
Feel free to open issues or submit pull requests if you have any improvements or bug fixes.

### Acknowledgement
- The Project took inspiration from the [reddit-saved-saver](https://github.com/tobiasvl/reddit-saved-saver).