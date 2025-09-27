import os
import pandas as pd
from tqdm import tqdm
from utils.file_operations import save_to_file
from utils.save_utils import save_submission, save_comment_and_context
from utils.time_utilities import dynamic_sleep
from utils.env_config import get_ignore_tls_errors

def get_gdpr_directory(save_directory):
    """Return the path to the GDPR data directory."""
    # This function only checks for the directory's existence and
    # prints a message if it's missing. Creation should be handled
    # by the caller.
    gdpr_dir = os.path.join(save_directory, 'gdpr_data')
    if not os.path.exists(gdpr_dir):
        print(f"GDPR data directory not found at: {gdpr_dir}")
    return gdpr_dir

def process_gdpr_export(reddit, save_directory, existing_files, created_dirs_cache, file_log):
    """Process saved posts/comments from GDPR export CSVs."""
    processed_count = 0
    skipped_count = 0
    total_size = 0

    # Load the ignore_tls_errors setting
    ignore_tls_errors = get_ignore_tls_errors()
    
    gdpr_dir = get_gdpr_directory(save_directory)
    
    # Process saved posts
    posts_file = os.path.join(gdpr_dir, 'saved_posts.csv')
    if os.path.exists(posts_file):
        print("\nProcessing saved posts from GDPR export...")
        df = pd.read_csv(posts_file)
        
        for _, row in tqdm(df.iterrows(), total=len(df), desc="Processing GDPR Posts"):
            try:
                # Get full submission data using the ID
                submission = reddit.submission(id=row['id'])
                file_path = os.path.join(save_directory, 
                                       submission.subreddit.display_name, 
                                       f"GDPR_POST_{submission.id}.md")
                
                # Use existing save_to_file function
                if save_to_file(submission, file_path, save_submission,
                              existing_files, file_log, save_directory, created_dirs_cache,
                              ignore_tls_errors=ignore_tls_errors):
                    skipped_count += 1
                    continue

                processed_count += 1
                total_size += os.path.getsize(file_path)
                dynamic_sleep(len(submission.selftext) if submission.is_self else 0)

            except Exception as e:
                print(f"Error processing GDPR post {row['id']}: {e}")
                skipped_count += 1

    # Process saved comments
    comments_file = os.path.join(gdpr_dir, 'saved_comments.csv')
    if os.path.exists(comments_file):
        print("\nProcessing saved comments from GDPR export...")
        df = pd.read_csv(comments_file)
        
        for _, row in tqdm(df.iterrows(), total=len(df), desc="Processing GDPR Comments"):
            try:
                # Get full comment data using the ID
                comment = reddit.comment(id=row['id'])
                file_path = os.path.join(save_directory, 
                                       comment.subreddit.display_name, 
                                       f"GDPR_COMMENT_{comment.id}.md")
                
                # Use existing save_to_file function
                if save_to_file(comment, file_path, save_comment_and_context,
                              existing_files, file_log, save_directory, created_dirs_cache,
                              ignore_tls_errors=ignore_tls_errors):
                    skipped_count += 1
                    continue

                processed_count += 1
                total_size += os.path.getsize(file_path)
                dynamic_sleep(len(comment.body))

            except Exception as e:
                print(f"Error processing GDPR comment {row['id']}: {e}")
                skipped_count += 1

    return processed_count, skipped_count, total_size 