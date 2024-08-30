# Reddit Stash

Reddit Stash is a Python script that automatically saves your Reddit saved posts and comments to your local machine or Dropbox. It uses GitHub Actions to run the script on a daily schedule for Dropbox.

## Features
- Downloads the saved Reddit folder from Dropbox.
- Automatically retrieves saved posts and comments from Reddit.
- Allows for flexible saving options (all activity or only saved items) via `settings.ini`.
- Uploads the files to Dropbox for storage.
- Saves the content as markdown files.

## Setup

### Prerequisites
- Python 3.10
- Reddit API credentials.
- A Dropbox account with an API token. (Optional)

### Installation

Before proceeding with any installation method, ensure that you have set the Reddit environment variables. Follow [this guide](#setting-up-reddit-environment-variables) to create a Reddit app and obtain the necessary credentials.

#### GitHub Action Installation

**Note:** The following process requires the [Dropbox App setup](#setting-up-dropbox-app). The GitHub Actions workflow runs the script daily at midnight CET, uploading the files to Dropbox. The workflow is defined in `.github/workflows/reddit_scraper.yml`.

1. **Fork this repository**.

2. **Set Up Secrets:**
- Go to your forked repository’s **Settings** > **Secrets and variables** > **Actions** > Click on **New repository secret**.
- Add the following secrets individually:
    - `REDDIT_CLIENT_ID`
    - `REDDIT_CLIENT_SECRET`
    - `REDDIT_USERNAME`
    For Dropbox Setup
    - `DROPBOX_APP_KEY`
    - `DROPBOX_APP_SECRET`
    - `DROPBOX_REFRESH_TOKEN`
- Enter the respective secret values without any quotes.

After adding all secrets: ![Repository Secrets](resources/repositiory_secrets.png).

3. **Manually Trigger the Workflow**:
- Go to the **Actions** tab > Select the **Reddit Stash Workflow** from the list on the left > Click **Run workflow** > Select the branch `main` > Click the green **Run workflow** button. The workflow will then be triggered, and you can monitor its progress in the Actions tab. Upon successful completion, you should see the Reddit folder in your Dropbox.

#### Local Installation

1. **Clone this repository**:
   ```
   git clone https://github.com/rhnfzl/reddit-stash.git
   cd reddit-stash
   ```

2. Install the required Python packages:
    ```
    pip install -r requirements.txt
    ```

3. Setup the [Dropbox App setup](#setting-up-dropbox-app). Skip it if you don't want to setup the dropbox and only want to save the file locally in your system.

4. Edit the settings.ini file, here is [how to](#`settings.ini`-file)

5. Set Environment Variables (Optional but preferred):

    For macOS and Linux:
    ```
    export REDDIT_CLIENT_ID='your_client_id'
    export REDDIT_CLIENT_SECRET='your_client_secret'
    export REDDIT_USERNAME='your_username'
    export REDDIT_PASSWORD='your_password'
    # Optional, if you need dropbox locally
    export DROPBOX_APP_KEY='dropbox-app-key'
    export DROPBOX_APP_SECRET='dropbox-secret-key'
    export DROPBOX_REFRESH_TOKEN='dropbox-secret-key'
    ```

    For Windows:

    ```
    set REDDIT_CLIENT_ID='your_client_id'
    set REDDIT_CLIENT_SECRET='your_client_secret'
    set REDDIT_USERNAME='your_username'
    set REDDIT_PASSWORD='your_password'
    # Optional, if you need dropbox locally
    set DROPBOX_APP_KEY='dropbox-app-key'
    set DROPBOX_APP_SECRET='dropbox-secret-key'
    set DROPBOX_REFRESH_TOKEN='dropbox-secret-key'
    ```
    
    You can verify the setup with:
    ```
    echo $REDDIT_CLIENT_ID
    echo $REDDIT_CLIENT_SECRET
    echo $REDDIT_USERNAME
    echo $REDDIT_PASSWORD
    echo $DROPBOX_APP_KEY
    echo $DROPBOX_APP_SECRET
    echo $DROPBOX_REFRESH_TOKEN
    ```

6. Usage:
    * First-time setup:
    ```
    python reddit_stash.py
    ```
    To upload to Dropbox (optional):
    ```
    python dropbox_utils.py --upload
    ```
    * Subsequent runs, as per your convenience:
    1. Download from Dropbox (optional):
    ```
    python dropbox_utils.py --download
    ```
    2. Process Reddit saved items:
    ```
    python reddit_stash.py
    ```
    3. Upload to Dropbox (optional):
    ```
    python dropbox_utils.py --upload
    ```
## Configuration

#### `settings.ini` File

The `settings.ini` file in the root directory of the project allows you to configure how Reddit Stash operates. Here’s what each section of the file does:

```ini
[Settings]
save_directory = reddit/ # your system save directory
dropbox_directory = /reddit # your dropbox directory
save_type = ALL  # Options: 'ALL' to save all activity, 'SAVED' to save only saved posts/comments

[Configuration]
client_id = None  # Can be set here or via environment variables
client_secret = None  # Can be set here or via environment variables
username = None  # Can be set here or via environment variables
password = None  # Can be set here or via environment variables
```
save_directory: Specifies the directory where the Reddit content will be saved, modify it to the location you want it to be in.
dropbox_directory : Specifies the folder where the Reddit content will be saved on dropbox, modify it to the location you want it to be in.
save_type: Determines what user activity is saved, accepts these two values:
* `ALL`: Saves all posts and comments made by the user, along with the saved posts and comments with it's context.
* `SAVED`: Saves only the posts and comments the user has saved on Reddit with it's context.

Note: You can still use environment variables as a fallback or override for the Reddit API credentials if they are not set in the settings.ini file.

#### Setting Up Reddit Environment Variables

* Create a Reddit app at https://old.reddit.com/prefs/apps/
* Set up the name, select `script`, and provide the `redirect_uri` as per the [PRAW docs](https://praw.readthedocs.io/en/latest/getting_started/authentication.html#password-flow).

![Step 1](resources/reddit_create_app1.png)

* Copy the provided `REDDIT_CLIENT_ID` and the `REDDIT_CLIENT_SECRET` based on the following screenshot:

![Step 2](resources/reddit_create_app2.png)

* `REDDIT_USERNAME` is your reddit username
* `REDDIT_PASSWORD` is your reddit passowrd
Keep these credentials for the setup.

#### Setting Up Dropbox app
* Go to [Dropbox Developer App](https://www.dropbox.com/developers/apps).
* Click on Create app.
* Select `Scoped access` and choose `Full Dropbox` or `App folder` for access type.
* give a Name to your app and click `Create app`.
![dropbox1](resources/dropbox_app1.png)
- In the `Permissions` tab, ensure the following are checked under `Files and folders`:
    * `files.metadata.write`
    * `files.metadata.read`
    * `files.content.write`
    * `files.content.read`
    * Click `Submit` in the bottom.
![dropbox2](resources/dropbox_app2.png)
* Your `DROPBOX_APP_KEY` and `DROPBOX_APP_SECRET` are in the settings page of the app you created.
![dropbox3](resources/dropbox_app3.png)
* To get the (`DROPBOX_REFRESH_TOKEN`)[https://stackoverflow.com/a/71794390/12983596] follow the follwing steps:

Replace `<DROPBOX_APP_KEY>` with your `DROPBOX_APP_KEY` you got in previous step and add that in the below Authorization URL

https://www.dropbox.com/oauth2/authorize?client_id=<DROPBOX_APP_KEY>&token_access_type=offline&response_type=code

Paste the URL in browser and complete the code flow on the Authorization URL. You will receive an `<AUTHORIZATION_CODE>` at the end, save it you will need this later.

Go to [Postman](https://www.postman.com/), and create a new POST request with below configuration

* Add Request URL- https://api.dropboxapi.com/oauth2/token
![postman1](resources/postman_post1.png)

* Click on the **Authorization** tab -> Type = **Basic Auth** -> **Username** = `<DROPBOX_APP_KEY>` , **Password** = `<DROPBOX_APP_SECRET>`
(Refer this [answer](https://stackoverflow.com/a/28529598/18744450) for cURL -u option)

![postman2](resources/postman_post2.png)

* Body -> Select "x-www-form-urlencoded"

|    Key   |      Value          |
|:--------:|:-------------------:|
|    code  |`<AUTHORIZATION_CODE>` |
|grant_type| authorization_code  |

![postman3](resources/postman_post3.png)

After you click send the request, you will receive JSON payload containing **refresh_token**.
```{
    "access_token": "sl.****************",
    "token_type": "bearer",
    "expires_in": 14400,
    "refresh_token": "*********************",
    "scope": <SCOPES>,
    "uid": "**********",
    "account_id": "***********************"
}```

and add/export the above r**refresh_token** to DROPBOX_REFRESH_TOKEN in your environment.
For more information about the setup visit [OAuth Guide](https://developers.dropbox.com/oauth-guide).


- Credits for above DROPBOX_REFRESH_TOKEN solution : https://stackoverflow.com/a/71794390/12983596

### Key Additions and Changes:

- **Configuration Section**: Added a new section explaining the `settings.ini` file and the `save_type` option.
- **Setup Instructions**: Provided guidance on editing the `settings.ini` file and clarifying the role of environment variables as a fallback.
- **Consistent Documentation**: Updated the usage instructions to reflect the new configuration options.

### Contributing
Feel free to open issues or submit pull requests if you have any improvements or bug fixes.

### Acknowledgement
- This project was inspired by [reddit-saved-saver](https://github.com/tobiasvl/reddit-saved-saver).

### Issues:

- ~~The dropbox isn't working at the moment because the token expiration, I need to find out a way to tackle that here, the main code `reddit_stash.py` works as expected.~~
- The dropbox code needs to have the hashing mechanism, to make the upload faster.
- The `reddit_stash.py` downloads all the file first and decides if the file is availble or not, implement early exit startegy while relevent fetching the content.
- The file size calculation should be done once rather than in each iterations.
