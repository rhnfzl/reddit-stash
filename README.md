# Reddit Stash: Automatically Save Reddit Posts and Comments to Dropbox

[![Python](https://img.shields.io/badge/Python-3.10-blue.svg?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Workflow-2088FF?style=for-the-badge&logo=github-actions&logoColor=white)](https://github.com/features/actions)
[![Dropbox](https://img.shields.io/badge/Dropbox-Integration-0061FF?style=for-the-badge&logo=dropbox&logoColor=white)](https://www.dropbox.com/)
[![Reddit](https://img.shields.io/badge/Reddit-API-FF4500?style=for-the-badge&logo=reddit&logoColor=white)](https://www.reddit.com/dev/api/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)

**Reddit Stash** is a Python script designed to help you effortlessly back up your Reddit **saved/ posted/ upvoted** posts and comments to Dropbox or your local machine. Utilizing GitHub Actions, this script runs daily, automating the process of archiving your Reddit data in Dropbox after a simple setup.

## 📋 What You Get

When Reddit Stash runs successfully, your saved content is organized by subreddit in a clean folder structure and stored as markdown files:

```
reddit/
├── r_AskReddit/
│   ├── POST_abcd123.md
│   └── COMMENT_efgh456.md
├── r_ProgrammerHumor/
│   └── POST_ijkl789.md
└── file_log.json
```

Each post and comment is formatted with:
- Original title and content
- Author information
- Post/comment URL
- Timestamp
- Subreddit details
- Any images or links from the original post

## Table of Contents

- [⚡ Quick Start](#-quick-start)
- [How It Works](#how-it-works)
- [Features](#features)
- [Installation Methods](#installation-methods)
  - [GitHub Actions Installation](#github-actions-installation)
  - [Local Installation](#local-installation)
  - [Docker Installation](#docker-installation)
  - [Docker Technical Details](#docker-technical-details)
- [GDPR Data Processing](#gdpr-data-processing)
  - [Setup Instructions](#setup-instructions)
  - [Technical Implementation](#technical-implementation)
  - [Benefits](#benefits)
- [File Organization and Utilities](#file-organization-and-utilities)
  - [Project Structure](#project-structure)
  - [Core Utilities](#core-utilities)
  - [Data Flow Between Components](#data-flow-between-components)
- [Command Reference](#command-reference)
  - [Basic Usage](#basic-usage)
  - [CLI Arguments](#cli-arguments)
  - [Environment Variables](#environment-variables)
  - [Docker Commands](#docker-commands)
  - [GitHub Actions Custom Run](#github-actions-custom-run)
- [Frequently Asked Questions](#frequently-asked-questions)
- [Setup Comparison](#setup-comparison)
- [Setup Verification Checklist](#setup-verification-checklist)
- [Troubleshooting](#troubleshooting)
- [Security Considerations](#security-considerations)
- [Future Enhancements](#future-enhancements)
- [License](#license)

## ⚡ Quick Start

For those who want to get up and running quickly, here's a streamlined process:

### Option 1: GitHub Actions (Easiest Setup)

1. Fork this repository.
2. Set up the required secrets in your GitHub repository:
   - From Reddit: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`
   - From Dropbox: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`
3. Manually trigger the workflow from the Actions tab.

### Option 2: Local Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/rhnfzl/reddit-stash.git
   cd reddit-stash
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up your environment variables and run:
   ```bash
   python reddit_stash.py
   ```

For detailed setup instructions, continue reading the [Setup](#setup) section.

### Setup Method Comparison

| Feature | GitHub Actions | Local Installation | Docker |
|---------|---------------|-------------------|--------|
| **Ease of Setup** | ⭐⭐⭐ (Easiest) | ⭐⭐ | ⭐⭐ |
| **Automation** | ✅ Runs on schedule | ❌ Manual or requires cron | ✅ Can be scheduled |
| **Requirements** | GitHub account | Python 3.10 | Docker |
| **Data Storage** | Dropbox required | Local or Dropbox | Local or Dropbox |
| **Maintenance** | Minimal | More hands-on | Medium |
| **Privacy** | Credentials in GitHub secrets | Credentials on local machine | Credentials in container |
| **Best For** | Set & forget users | Power users with customization needs | Users with existing Docker infrastructure |

## Key Features

- 🤖 **Automated Reddit Backup:** Automatically retrieves saved posts and comments from Reddit, even your posts and comments if you set it up.
- 🔄 **Flexible Storage Options:** Allows for flexible saving options (all activity or only saved items) via `settings.ini`.
- 📦 **Dropbox Integration:** Downloads and uploads the files to Dropbox for storage.
- 📝 **Markdown Support:** Saves the content as markdown files.
- 🔍 **File Deduplication:** Uses intelligent file existence checking to avoid re-downloading content.
- ⏱️ **Rate Limit Management:** Implements dynamic sleep timers to respect Reddit's API rate limits.
- 🔒 **GDPR Data Processing:** Optional processing of Reddit's GDPR export data.

## 🎯 Why Use Reddit Stash

Reddit Stash was designed with specific use cases in mind:

### 1. Overcome Reddit's Limitations
Reddit only shows your most recent 1000 saved posts. With Reddit Stash, you can save everything and go beyond this limitation.

### 2. Create a Personal Knowledge Base
Many users save technical posts, tutorials, or valuable discussions on Reddit. Reddit Stash helps you build a searchable archive of this knowledge.

### 3. Preserve Content Before It's Deleted
Reddit posts and comments can be deleted by users or moderation. Reddit Stash preserves this content in your personal archive.

### 4. Access Your Content Offline
All of your saved posts are available locally in markdown format, making them easily accessible even without an internet connection.

### 5. Integration with Note-Taking Systems
Since content is saved in markdown, you can easily import it into note-taking systems like Obsidian, Notion, or any markdown-compatible tool.

## Setup

### Prerequisites
- ✅ Python 3.10
- 🔑 Reddit API credentials
- 📊 A Dropbox account with an API token

### Installation

Before proceeding with any installation method, ensure that you have set the Reddit environment variables. Follow [Reddit API guide](#setting-up-reddit-environment-variables) to create a Reddit app and obtain the necessary credentials.

#### GitHub Action Installation (Recommended)

**Note:** The following process requires the [Dropbox App setup](#setting-up-dropbox-app). The GitHub Actions workflow runs the script automatically on a schedule, uploading the files to Dropbox. The workflow is defined in `.github/workflows/reddit_scraper.yml`.

1. **Fork this repository**.

2. **Set Up Secrets:**
- Go to your forked repository's **Settings** > **Secrets and variables** > **Actions** > Click on **New repository secret**.
- Add the following secrets individually:
    - `REDDIT_CLIENT_ID`
    - `REDDIT_CLIENT_SECRET`
    - `REDDIT_USERNAME`
    - `REDDIT_PASSWORD` (needed for GitHub Actions)
For Dropbox Setup
    - `DROPBOX_APP_KEY`
    - `DROPBOX_APP_SECRET`
    - `DROPBOX_REFRESH_TOKEN`
- Enter the respective secret values without any quotes.

After adding all secrets: ![Repository Secrets](resources/repositiory_secrets.png).

3. **Manually Trigger the Workflow**:
- Go to the **Actions** tab > Select the **Reddit Stash Workflow** from the list on the left > Click **Run workflow** > Select the branch `main` > Click the green **Run workflow** button. The workflow will then be triggered, and you can monitor its progress in the Actions tab. Upon successful completion, you should see the Reddit folder in your Dropbox.

4. **Workflow Process**:
   - The GitHub Actions workflow performs these steps in sequence:
     1. Downloads the log file from Dropbox to avoid reprocessing items
     2. Processes Reddit content based on your settings
     3. Uploads only modified files back to Dropbox
   - The workflow runs automatically on a schedule:
     - Every 2 hours during *peak hours* (8:00-23:00 CET time in summer)
     - Twice during *off-peak hours* (1:00 and 5:00 CET time in summer)
     - You can adjust these times in the workflow file to match your timezone if needed.

#### Local Installation

1. **Clone this repository**:
   ```
   git clone https://github.com/rhnfzl/reddit-stash.git
   cd reddit-stash
   ```

2. **Install dependencies**:
    ```
    pip install -r requirements.txt
    ```

3. **Configure settings**:
   - Edit the `settings.ini` file to match your preferences (see [Settings.ini File](#settingsini-file))
   - Setup the [Dropbox App](#setting-up-dropbox-app) if you want to use Dropbox integration

4. **Set Environment Variables**:

    For macOS and Linux:
    ```
    export REDDIT_CLIENT_ID='your_client_id'
    export REDDIT_CLIENT_SECRET='your_client_secret'
    export REDDIT_USERNAME='your_username'
    export REDDIT_PASSWORD='your_password'
    # Optional, if you need dropbox locally
    export DROPBOX_APP_KEY='dropbox-app-key'
    export DROPBOX_APP_SECRET='dropbox-secret-key'
    export DROPBOX_REFRESH_TOKEN='dropbox-refresh-token'
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
    set DROPBOX_REFRESH_TOKEN='dropbox-refresh-token'
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

5. **Run the script**:
   The script flow has these main steps:
   1. Loads configuration from `settings.ini` and environment variables
   2. Sets up the Reddit API connection
   3. Processes API-accessible items (saved/posted/upvoted depending on settings)
   4. Optionally processes GDPR export data if enabled
   5. Saves content as markdown files in the specified directory

   ```bash
   # Basic usage - process Reddit content according to settings.ini
   python reddit_stash.py
   
   # If using Dropbox, you can sync files
   # Download existing files from Dropbox first
   python dropbox_utils.py --download
   
   # Process Reddit content
   python reddit_stash.py
   
   # Upload to Dropbox
   python dropbox_utils.py --upload
   ```

#### Docker Installation

Docker provides an isolated environment to run Reddit Stash, ensuring consistent behavior across different systems.

1. **Build the Docker image**:
   ```bash
   docker build -t reddit-stash .
   ```

2. **Run the container**:
   The Docker container is configured to run the main script by default, but can also run the Dropbox utilities with different parameters.

   **Basic usage (Reddit processing)**:
   ```bash
   docker run -it \
     -e REDDIT_CLIENT_ID=your_client_id \
     -e REDDIT_CLIENT_SECRET=your_client_secret \
     -e REDDIT_USERNAME=your_username \
     -e REDDIT_PASSWORD=your_password \
     -e DROPBOX_APP_KEY=your_dropbox_key \
     -e DROPBOX_APP_SECRET=your_dropbox_secret \
     -e DROPBOX_REFRESH_TOKEN=your_dropbox_token \
     -v $(pwd)/reddit:/app/reddit \
     reddit-stash
   ```

   **For Dropbox download operation**:
   ```bash
   docker run -it \
     -e DROPBOX_APP_KEY=your_dropbox_key \
     -e DROPBOX_APP_SECRET=your_dropbox_secret \
     -e DROPBOX_REFRESH_TOKEN=your_dropbox_token \
     -v $(pwd)/reddit:/app/reddit \
     reddit-stash dropbox_utils.py --download
   ```

   **For Dropbox upload operation**:
   ```bash
   docker run -it \
     -e DROPBOX_APP_KEY=your_dropbox_key \
     -e DROPBOX_APP_SECRET=your_dropbox_secret \
     -e DROPBOX_REFRESH_TOKEN=your_dropbox_token \
     -v $(pwd)/reddit:/app/reddit \
     reddit-stash dropbox_utils.py --upload
   ```

   For Windows Command Prompt, use `^` for line continuation and `%cd%` instead of `$(pwd)`.

#### Docker Technical Details:

- **Container Structure**:
  - Uses Python 3.10 as the base image
  - Runs as a non-root user for security
  - Has an ENTRYPOINT/CMD configuration for flexible script execution
  - Maps a volume from your local machine to the container for persistent storage

- **Data Persistence**:
  - All Reddit content is stored in a volume mount (`-v $(pwd)/reddit:/app/reddit`)
  - This ensures data is saved to your local machine, not just inside the container
  - The file_log.json is also stored in this volume for efficient processing across runs

- **Environment Variables**:
  - All configuration is passed via environment variables
  - No credentials are stored in the container image
  - Variables are used only during runtime

- **Running Options**:
  - Interactive mode (`-it`) shows output in real-time
  - Background mode (`-d`) runs silently; check logs with `docker logs <container_id>`
  - Can be scheduled with external tools like cron or Task Scheduler

### Setup Verification Checklist

After completing your chosen installation method, verify that everything is working correctly:

#### For GitHub Actions Setup:
- [ ] Repository forked successfully
- [ ] All required secrets added to repository settings
- [ ] Workflow manually triggered at least once
- [ ] Workflow completes without errors (check Actions tab)
- [ ] Reddit folder appears in your Dropbox account
- [ ] Content files are present and readable

#### For Local Installation:
- [ ] Python 3.10 installed and working
- [ ] Repository cloned successfully
- [ ] Dependencies installed via `pip install -r requirements.txt`
- [ ] Environment variables set correctly
- [ ] Script runs without errors
- [ ] Content saved to specified directory
- [ ] (Optional) Content uploaded to Dropbox if configured

#### For Docker Installation:
- [ ] Docker installed and daemon running
- [ ] Image built successfully
- [ ] Container runs without errors
- [ ] Content appears in mounted volume
- [ ] (Optional) Content uploaded to Dropbox if configured

## Configuration

#### `settings.ini` File

The `settings.ini` file in the root directory of the project allows you to configure how Reddit Stash operates. Here's what each section of the file does:

```ini
[Settings]
save_directory = reddit/ # your system save directory
dropbox_directory = /reddit # your dropbox directory
save_type = ALL  # Options: 'ALL' to save all activity, 'SAVED' to save only saved posts/comments, 'ACTIVITY' to save only the users posts and comments, 'UPVOTED' to save users upvoted post and comments
check_type = LOG # Options: 'LOG' to use the logging file to verify the file exisitnece, 'DIR' to verify the file exisitence based on the downloaded directory. 
unsave_after_download = false
process_gdpr = false # Whether to process GDPR export data
process_api = true # Whether to process items from Reddit API (default: true)

[Configuration]
client_id = None  # Can be set here or via environment variables
client_secret = None  # Can be set here or via environment variables
username = None  # Can be set here or via environment variables
password = None  # Can be set here or via environment variables
```

#### Settings Explained:

* **save_directory**: Specifies the directory where the Reddit content will be saved, modify it to the location you want it to be in.
* **dropbox_directory**: Specifies the folder where the Reddit content will be saved on dropbox, modify it to the location you want it to be in.
* **save_type**: Determines what user activity is saved:
    * `ALL`: Saves all posts and comments made by the user, the saved posts and comments with their context, along with the upvoted posts and comments.
    * `SAVED`: Saves only the posts and comments the user has saved on Reddit with their context.
    * `ACTIVITY`: Saves only the posts and comments user has made/posted on Reddit with their context.
    * `UPVOTED`: Saves only the posts and comments the user has upvoted with their context.
* **check_type**: Determines how file existence is checked:
    * `LOG`: Uses only the log file to check for file existence (faster processing). Recommended for GitHub Actions setup.
    * `DIR`: Checks the saved/downloaded directory for file existence (slower but more thorough). Recommended for local setup.
* **unsave_after_download**: When set to `true`, automatically unsaves posts after downloading them (see notes below).
* **process_gdpr**: When set to `true`, processes GDPR export data (explained in detail below).
* **process_api**: When set to `true` (default), processes items from the Reddit API.

Note: You can still use environment variables as a fallback or override for the Reddit API credentials if they are not set in the settings.ini file.

#### Setting Up Reddit Environment Variables

* Create a Reddit app at https://www.reddit.com/prefs/apps or https://old.reddit.com/prefs/apps/
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
* To get the `DROPBOX_REFRESH_TOKEN` follow the follwing steps:

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
```
{
    "access_token": "sl.****************",
    "token_type": "bearer",
    "expires_in": 14400,
    "refresh_token": "*********************",
    "scope": <SCOPES>,
    "uid": "**********",
    "account_id": "***********************"
}
```

and add/export the above r**refresh_token** to DROPBOX_REFRESH_TOKEN in your environment.
For more information about the setup visit [OAuth Guide](https://developers.dropbox.com/oauth-guide).


- Credits for above DROPBOX_REFRESH_TOKEN solution : https://stackoverflow.com/a/71794390/12983596

## Important Notes

### Important Note About Unsaving

⚠️ **The script includes an option to automatically unsave posts after downloading them (`unsave_after_download` in settings.ini). This feature can be used to cycle through older saved posts beyond Reddit's 1000-item limit.**

#### How it works:
1. The script downloads and saves a post/comment
2. If successful, it attempts to unsave the item
3. A small delay is added between unsave operations to respect Reddit's rate limits
4. Error handling ensures that failed unsaves don't stop the script

#### Important Considerations:
- **This process is irreversible** - Once items are unsaved, they cannot be automatically restored to your saved items list
- **Create backups first** - Always ensure you have a backup of your saved items before enabling this feature
- **Use with caution** - It's recommended to first run the script without unsaving to verify everything works as expected
- **Rate Limiting** - The script includes built-in delays to avoid hitting Reddit's API limits
- **Error Recovery** - If an unsave operation fails, the script will continue processing other items

#### Usage:
1. Set `unsave_after_download = true` in your settings.ini file
2. Run the script as normal
3. The script will now unsave items after successfully downloading them
4. Run the script multiple times to gradually access older saved items

#### Recommended Workflow:
1. First run: Keep `unsave_after_download = false` and verify all content downloads correctly
2. Create a backup of your downloaded content
3. Enable unsaving by setting `unsave_after_download = true`
4. Run the script multiple times to access progressively older content

### GDPR Data Processing

Reddit Stash can process Reddit's GDPR data export to access your complete saved post history. This feature is particularly valuable because it lets you access posts saved beyond Reddit's 1000-item limit.

#### How GDPR Processing Works:

1. The script reads your GDPR export CSV files
2. For each item in the export, it fetches the full content using Reddit's API
3. Content is formatted and saved following the same structure as regular API-fetched items
4. Files are prefixed with "GDPR_" to distinguish them from regular API-fetched content

#### Setting Up GDPR Processing:

1. **Request your Reddit data export**:
   - Navigate to [Reddit's Data Request page](https://www.reddit.com/settings/data-request)
   - Request your data (processing by Reddit typically takes 1-3 days)
   - Download the ZIP file when you receive the email notification

2. **Prepare the data for processing**:
   - Extract the ZIP file you received from Reddit
   - Inside the Reddit GDPR export, locate the following files:
     - `saved_posts.csv`: Contains your saved posts history
     - `saved_comments.csv`: Contains your saved comments history
   - Create a `gdpr_data` folder inside your save directory (specified in settings.ini)
   - Copy these CSV files into the `gdpr_data` folder

3. **Directory structure should look like this**:
   ```
   reddit/                  # Your save directory as specified in settings.ini
   ├── gdpr_data/           # GDPR data directory
   │   ├── saved_posts.csv  # From Reddit's GDPR export
   │   └── saved_comments.csv  # From Reddit's GDPR export
   ├── r_subreddit1/        # Regular content folders
   ├── r_subreddit2/
   └── file_log.json        # Processing log
   ```

4. **Enable GDPR processing in settings.ini**:
   ```ini
   [Settings]
   process_gdpr = true      # Set to true to process GDPR data
   process_api = true       # Whether to also process API content (can be set to false if you only want GDPR)
   ```

5. **Run the script**:
   ```bash
   python reddit_stash.py
   ```

#### Technical Implementation Details:

- The GDPR processor module (`utils/gdpr_processor.py`):
  - Reads CSV files using pandas
  - Retrieves each item's full content via Reddit's API
  - Formats content consistently with the main script's output 
  - Maintains the same folder structure organization by subreddit
  - Uses the same file existence checking to avoid duplicates
  - Adds content to the file_log.json for tracking

- **Rate Limiting**:
  - Implements the same dynamic sleep strategy used in the main script
  - Respects Reddit's API rate limits automatically
  - Handles errors gracefully with retries when needed

- **Error Handling**:
  - Continues processing if individual items fail
  - Skips items that no longer exist on Reddit
  - Logs processing statistics separate from regular API processing

#### Benefits of GDPR Processing:

- Access to your **entire Reddit save history** beyond the 1000-item API limit
- Historical data preservation, including content from deleted/quarantined subreddits
- Integration with the full Reddit Stash workflow, including Dropbox backup
- Complete record of your saved content activity over time

The GDPR export processing is particularly useful for users with extensive saved content history who want to preserve their complete Reddit saved items archive.

### File Organization and Utilities

Reddit Stash organizes content by subreddit with a clear file naming convention:

- **Posts**: `POST_[post_id].md` or `GDPR_POST_[post_id].md`
- **Comments**: `COMMENT_[comment_id].md` or `GDPR_COMMENT_[comment_id].md`

#### Project Structure

```
reddit-stash/
├── reddit_stash.py            # Main script for processing Reddit content
├── dropbox_utils.py           # Dropbox upload/download functionality
├── settings.ini               # Configuration file
├── requirements.txt           # Python dependencies
├── utils/                     # Utility modules
│   ├── file_operations.py     # Core file handling functions
│   ├── save_utils.py          # Content formatting and saving
│   ├── gdpr_processor.py      # GDPR export processing
│   ├── time_utilities.py      # Rate limiting and sleep timing
│   ├── log_utils.py           # File logging for deduplication
│   ├── env_config.py          # Environment variable handling
│   └── file_path_validate.py  # Path validation utilities
└── .github/workflows/         # GitHub Actions workflow definitions
```

#### Core Utilities

Each utility module has a specific purpose in the Reddit Stash workflow:

**file_operations.py**:
- Creates and manages directory structure for saved content
- Implements file existence checking to avoid duplicates
- Handles the core saving logic for different content types
- Manages the different save types (ALL, SAVED, ACTIVITY, UPVOTED)
- Implements unsaving functionality when enabled

**save_utils.py**:
- Formats posts and comments into markdown
- Includes metadata like URLs, timestamps, and authors
- Renders comment threads with appropriate indentation
- Downloads images for posts when possible
- Extracts video IDs from supported platforms

**gdpr_processor.py**:
- Parses CSV files from Reddit's GDPR export
- Maps GDPR data to Reddit API objects
- Uses the same saving functions as the main script
- Maintains consistent file naming conventions

**time_utilities.py**:
- Implements dynamic sleep timers to respect Reddit's API rate limits
- Adjusts timing based on response headers
- Prevents rate limit errors by proactively slowing down requests
- Handles throttling with exponential backoff when needed

**log_utils.py**:
- Maintains a JSON log of all saved files
- Enables efficient checking for duplicates
- Records metadata about each processed file
- Persists between runs to prevent redundant processing

**env_config.py**:
- Loads configuration from environment variables
- Provides fallback to settings.ini values
- Validates required credentials
- Ensures secure credential handling

**file_path_validate.py**:
- Ensures paths are safe and properly formatted
- Creates directories when needed
- Sanitizes file and directory names
- Handles platform-specific path issues

#### Data Flow Between Components

1. **Initial Configuration**:
   - `settings.ini` defines the behavior of the script
   - `env_config.py` loads and validates credentials
   - `file_path_validate.py` prepares directories

2. **Content Retrieval**:
   - Main script fetches content from Reddit API
   - `time_utilities.py` ensures rate limits are respected
   - GDPR processor handles GDPR export data if enabled

3. **Content Processing**:
   - `save_utils.py` formats content as markdown
   - `file_operations.py` organizes by subreddit
   - `log_utils.py` tracks processed files

4. **Storage Integration**:
   - Local files are saved in the directory structure
   - `dropbox_utils.py` handles Dropbox integration
   - GitHub Actions orchestrates the workflow

This modular design makes the code maintainable and allows for easy updates to individual components without affecting the entire system.

## Frequently Asked Questions

### General Questions

**Q: Why would I want to backup my Reddit content?**  
A: Reddit only allows you to access your most recent 1000 saved items. This tool lets you preserve everything beyond that limit and ensures you have a backup even if content is removed from Reddit.

**Q: How often does the automated backup run?**  
A: If you use the GitHub Actions setup, it runs on a schedule:
- Every 2 hours during peak hours (8:00-23:00 CET time in summer)
- Twice during off-peak hours (1:00 and 5:00 CET time in summer)

**Q: Can I run this without GitHub Actions?**  
A: Yes, you can run it locally on your machine or set up the Docker container version. The README provides instructions for both options.

### Technical Questions

**Q: Does this access private/NSFW subreddits I've saved content from?**  
A: Yes, as long as you're logged in with your own Reddit credentials, the script can access any content you've saved, including from private or NSFW subreddits.

**Q: How can I verify the script is working correctly?**  
A: Check your specified save directory for the backed-up files. They should be organized by subreddit with clear naming conventions.

**Q: Will this impact my Reddit account in any way?**  
A: No, unless you enable the `unsave_after_download` option. This script only reads your data by default; it doesn't modify anything on Reddit unless that specific option is enabled.

**Q: What happens if the script encounters rate limits?**  
A: The script has built-in dynamic sleep timers to respect Reddit's API rate limits. It will automatically pause and retry when necessary.

## 🔧 Troubleshooting

If you encounter issues with Reddit Stash, here are solutions to common problems:

### Authentication Issues

**Problem**: "Invalid credentials" or "Authentication failed" errors
- **Solution**: 
  1. Double-check your Reddit API credentials
  2. Ensure your Reddit account is verified with an email address
  3. Make sure your app is properly set up with the correct redirect URI
  4. Verify that your password is correct (for local installations)

### Rate Limiting

**Problem**: "Too many requests" or frequent pauses during execution
- **Solution**: 
  1. This is normal behavior to respect Reddit's API limits
  2. The script will automatically slow down and retry
  3. For larger archives, consider running at off-peak hours
  4. Try reducing the frequency of scheduled runs in GitHub Actions

### Empty Results

**Problem**: Script runs successfully but no files are saved
- **Solution**: 
  1. Verify that your Reddit account has saved posts/comments
  2. Check your `settings.ini` file to ensure the correct `save_type` is selected
  3. Look at the console output for any warnings or errors
  4. Make sure your file paths in settings.ini are correct

### Dropbox Issues

**Problem**: Files aren't appearing in Dropbox
- **Solution**: 
  1. Verify your Dropbox API credentials and refresh token
  2. Check that your Dropbox app has the correct permissions
  3. Run `python dropbox_utils.py --upload` manually to test the upload
  4. Look for error messages during the upload process

### GitHub Actions Workflow Failures

**Problem**: GitHub Actions workflow fails
- **Solution**: 
  1. Check the workflow logs for detailed error messages
  2. Verify all required secrets are set correctly
  3. Make sure your Dropbox token hasn't expired
  4. Check for changes in the Reddit API that might affect the script

If you're experiencing issues not covered here, please open an issue on GitHub with details about the problem and any error messages you received.

## 🔐 Security Considerations

When using Reddit Stash, keep these security considerations in mind:

### API Credentials

- **Never share your Reddit API credentials** or Dropbox tokens with others
- When using GitHub Actions, your credentials are stored as encrypted secrets
- For local installations, consider using environment variables instead of hardcoding credentials in the settings file
- Regularly rotate your API keys and tokens, especially if you suspect they may have been compromised

### Content Security

- Reddit Stash downloads and stores all content from saved posts, including links and images
- Be aware that this may include sensitive or private information if you've saved such content
- Consider where you're storing the backed-up content and who has access to that location
- Dropbox encryption provides some protection, but for highly sensitive data, consider additional encryption

### GitHub Actions Security

- The GitHub Actions workflow runs in GitHub's cloud environment
- While GitHub has strong security measures, be aware that your Reddit content is processed in this environment
- The workflow has access to your repository secrets and the content being processed
- For maximum security, consider running the script locally on a trusted machine

### Local Storage Considerations

- Content is stored in plain text markdown files
- If storing content locally, ensure your device has appropriate security measures (encryption, access controls)
- If you back up your local storage to other services, be mindful of where your Reddit content might end up

## Contributing

Feel free to open issues or submit pull requests if you have any improvements or bug fixes.

### Acknowledgement
- This project was inspired by [reddit-saved-saver](https://github.com/tobiasvl/reddit-saved-saver).

## Project Status

### Resolved Issues
✅ The dropbox authentication now works correctly with refresh tokens  
✅ The script implements early exit strategy while fetching content for better efficiency  
✅ Added Docker Image support to run it on Local/NAS systems  
✅ Added processing of the GDPR export data from Reddit

### Future Enhancements

The Reddit Stash project is continuously evolving. Here are some planned enhancements and potential future features:

### Near-Term Improvements

- **Enhanced Content Formatting**
  - Better handling of complex markdown content
  - Improved image and video embedding
  - Support for code syntax highlighting in technical subreddits

- **Expanded Search Capabilities**
  - Full-text search across all saved content
  - Advanced filtering by subreddit, content type, and date ranges
  - Integration with local search tools

- **User Interface Options**
  - Simple web UI for browsing saved content
  - Content viewer with original Reddit formatting
  - Statistics dashboard for monitoring collection growth

### Medium-Term Goals

- **Additional Backup Providers**
  - Support for Google Drive integration
  - OneDrive/Microsoft 365 support
  - Direct S3/B2/other cloud storage integration

- **Content Enhancement**
  - Automatic content summarization
  - Tag generation for improved organization
  - Related content suggestions

- **Advanced Processing**
  - Comment thread preservation with full context
  - Automatic categorization by topic
  - Sentiment analysis and content classification

### Long-Term Vision

- **Content Analysis Tools**
  - Topic modeling across your saved content
  - Personalized recommendation engine
  - Timeline visualization of saved content

- **Integration Possibilities**
  - Discord bot for saved content browsing
  - API for third-party tool integration
  - Browser extension for enhanced saving

- **Content Preservation**
  - Archived web page snapshots
  - Media transcoding for long-term compatibility
  - Full website mirroring for complete context

### Community-Suggested Features

We welcome feature suggestions! Some popular requests include:

- Multi-account support for aggregating content
- Custom template system for markdown formatting
- Metadata enrichment from external sources
- WebDAV/NextCloud integration for self-hosted storage
- Cross-platform GUI application

## License

This project is licensed under the MIT License - see the LICENSE file for details.

This means you are free to:
- Use the software for commercial purposes
- Modify the software
- Distribute the software
- Use the software privately

With the condition that you include the original copyright notice and license in any copy of the software/source.

## Command Reference

### Basic Usage

```bash
# Run with default settings (uses settings.ini)
python reddit_stash.py

# Run with environment variables override
REDDIT_USERNAME=your_username REDDIT_PASSWORD=your_password python reddit_stash.py

# Docker run command (basic)
docker run --rm -v "$(pwd)/data:/app/data" reddit-stash
```

### CLI Arguments

Reddit Stash accepts several command-line arguments to modify behavior without changing the settings file:

```bash
# Override save type
python reddit_stash.py --save-type SAVED

# Process GDPR data only
python reddit_stash.py --gdpr-only

# API process only (no GDPR)
python reddit_stash.py --api-only

# Disable unsaving
python reddit_stash.py --no-unsave

# Enable debug mode
python reddit_stash.py --debug

# Custom save directory
python reddit_stash.py --save-dir /path/to/custom/directory

# Specify custom settings file
python reddit_stash.py --config custom_settings.ini

# Run Dropbox operations only (upload/download)
python reddit_stash.py --dropbox-only

# Combine multiple options
python reddit_stash.py --save-type ALL --no-unsave --debug
```

### Environment Variables

All settings can be overridden with environment variables:

| Environment Variable | Description | Example |
|---------------------|-------------|---------|
| `REDDIT_CLIENT_ID` | Reddit API client ID | `abcd1234efgh5678` |
| `REDDIT_CLIENT_SECRET` | Reddit API client secret | `ijkl9012mnop3456` |
| `REDDIT_USERNAME` | Reddit username | `your_username` |
| `REDDIT_PASSWORD` | Reddit password | `your_password` |
| `DROPBOX_APP_KEY` | Dropbox API app key | `abcd1234efgh5678` |
| `DROPBOX_APP_SECRET` | Dropbox API app secret | `ijkl9012mnop3456` |
| `DROPBOX_REFRESH_TOKEN` | Dropbox refresh token | `qrst5678uvwx9012` |
| `SAVE_DIRECTORY` | Directory to save files | `/home/user/reddit-data` |
| `SAVE_TYPE` | Type of content to save | `ALL`, `SAVED`, `ACTIVITY`, `UPVOTED` |
| `PROCESS_API` | Whether to process Reddit API | `true`, `false` |
| `PROCESS_GDPR` | Whether to process GDPR data | `true`, `false` |
| `UNSAVE_AFTER_DOWNLOAD` | Whether to unsave items | `true`, `false` |
| `DEBUG_MODE` | Enable verbose logging | `true`, `false` |

### Docker Commands

```bash
# Build the Docker image
docker build -t reddit-stash .

# Run with mounted volume for data persistence
docker run --rm -v "$(pwd)/data:/app/data" reddit-stash

# Run with environment variables
docker run --rm \
  -v "$(pwd)/data:/app/data" \
  -e REDDIT_USERNAME=your_username \
  -e REDDIT_PASSWORD=your_password \
  -e SAVE_TYPE=ALL \
  reddit-stash

# Run with custom command
docker run --rm \
  -v "$(pwd)/data:/app/data" \
  reddit-stash python reddit_stash.py --debug --no-unsave

# Run Dropbox operations only
docker run --rm \
  -v "$(pwd)/data:/app/data" \
  -e DROPBOX_APP_KEY=your_key \
  -e DROPBOX_APP_SECRET=your_secret \
  -e DROPBOX_REFRESH_TOKEN=your_token \
  reddit-stash python reddit_stash.py --dropbox-only
```

### GitHub Actions Custom Run

You can manually trigger the GitHub Actions workflow with custom parameters:

1. Go to the Actions tab in your forked repository
2. Select the "Reddit Stash" workflow
3. Click "Run workflow"
4. Fill in the custom parameters in the form
5. Click "Run workflow" to start the process

## Security Considerations