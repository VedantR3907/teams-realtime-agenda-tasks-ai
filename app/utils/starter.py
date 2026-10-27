from loguru import logger
from app.config import settings
import subprocess
import threading
import datetime
import platform
import psutil
import os

def log_output_reader(process, log_file_path):
    """
    Reads output from the process and writes it to both console and log file with timestamps.
    """
    try:
        with open(log_file_path, 'a', encoding='utf-8') as log_file:
            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    # Get current timestamp
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    # Clean the output (remove trailing newlines for formatting)
                    clean_output = output.strip()
                    
                    if clean_output:  # Only log non-empty lines
                        # Write to console (visible in CMD)
                        logger.info(f"[MEETING_BOT] {clean_output}")
                        
                        # Write to log file with timestamp
                        log_file.write(f"[{timestamp}] {clean_output}\n")
                        log_file.flush()
                        
    except Exception as e:
        logger.error(f"Error in log output reader: {e}")


def start_bot_process():
    """
    Starts the MeetingBot.exe process in C:/app using PowerShell commands, unless already running.
    Passes the meeting ID as a command-line argument: --AppSettings:MeetingId=<MEETING_ID>
    """
    if not settings.MEETING_ID:
        logger.info("Meeting ID is required to start the bot.")
        return

    if platform.system() != "Windows":
        logger.error("start_bot_process is only supported on Windows.")
        return

    try:
        # Check if MeetingBot.exe is already running
        for proc in psutil.process_iter(['name', 'exe', 'cmdline']):
            try:
                if (
                    proc.info['name'] and 'meetingbot.exe' in proc.info['name'].lower()
                ) or (
                    proc.info['exe'] and 'meetingbot.exe' in proc.info['exe'].lower()
                ) or (
                    proc.info['cmdline'] and any('meetingbot.exe' in str(arg).lower() for arg in proc.info['cmdline'])
                ):
                    logger.info("MeetingBot.exe is already running. Skipping start.")
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        # Get working directory from settings (which can be overridden by environment variables)
        working_directory = settings.BOT_WORKING_DIRECTORY
        
        # Derive bot executable path from working directory
        bot_exe_path = os.path.join(working_directory, 'MeetingBot.exe')

        # Check if the executable exists before trying to run it
        if not os.path.exists(bot_exe_path):
            logger.error(f"Could not find MeetingBot.exe at {bot_exe_path}")
            return
        
        # Create log file path in the same directory as the bot
        log_file_path = os.path.join(working_directory, "MeetingBot.log")
        
        # Launch the process with output redirected to log file
        with open(log_file_path, 'a', encoding='utf-8') as log_file:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_file.write(f"\n{'='*70}\n")
            log_file.write(f"[{timestamp}] MeetingBot.exe session started\n")
            log_file.write(f"{'='*70}\n")
            log_file.flush()
        
        # Start the process with output captured via PIPE
        process = subprocess.Popen(
            [bot_exe_path], 
            cwd=working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Redirect stderr to stdout
            universal_newlines=True,  # Handle text mode
            bufsize=1,  # Line buffered
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        )
        
        # Start a thread to read and log output
        log_thread = threading.Thread(
            target=log_output_reader, 
            args=(process, log_file_path),
            daemon=True  # Thread will close when main process closes
        )
        log_thread.start()
        
        logger.info(f"MeetingBot.exe started as a background process from {working_directory}")
        logger.info(f"Bot logs will be visible in CMD and written to: {log_file_path} 📝✨")


    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        # This can happen if a process terminates while iterating
        pass
    except Exception as e:
        logger.error(f"Failed to start MeetingBot.exe: {e}")

def stop_bot_process():
    """
    Stops the MeetingBot.exe process if it is running.
    """
    if platform.system() == "Windows":
        try:
            for proc in psutil.process_iter(['name', 'exe', 'cmdline']):
                try:
                    if (
                        proc.info['name'] and 'meetingbot.exe' in proc.info['name'].lower()
                    ) or (
                        proc.info['exe'] and 'meetingbot.exe' in proc.info['exe'].lower()
                    ) or (
                        proc.info['cmdline'] and any('meetingbot.exe' in str(arg).lower() for arg in proc.info['cmdline'])
                    ):
                        logger.info(f"Found MeetingBot.exe process with PID {proc.pid}. Terminating...")
                        proc.terminate()
                        proc.wait(timeout=5)
                        logger.info("MeetingBot.exe process terminated.")
                        return
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
                except psutil.TimeoutExpired:
                    logger.warning(f"MeetingBot.exe process with PID {proc.pid} did not terminate gracefully. Forcing kill...")
                    proc.kill()
                    logger.info("MeetingBot.exe process killed.")
                    return
            logger.info("MeetingBot.exe is not running.")
        except Exception as e:
            logger.error(f"Failed to stop MeetingBot.exe: {e}")
    else:
        logger.error("stop_bot_process is only supported on Windows.")