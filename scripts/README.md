# Useful Scripts

- [tampermonkey/bdfr-export.user.js](tampermonkey/bdfr-export.user.js): the userscript that sends posts from old.reddit.com to `bdfr serve` and exports them for `--post-json`; see the [README](../README.md#downloading-from-the-browser). Its Playwright tests are run with `npm install && npx playwright install chromium && npm test` in `tampermonkey/`.
- Log scripts, below: BDFR's logs are recurring patterns of strings, so a lot can be read from them with ordinary shell tools. Each has a Bash (`.sh`) and a PowerShell (`.ps1`) version, and they work on `log_output.txt` (from `bdfr download`) and `serve_log.txt` (from `bdfr serve`) in BDFR's configuration directory.

  - [Script to extract all successfully downloaded IDs](#extract-all-successfully-downloaded-ids)
  - [Script to extract all failed download IDs](#extract-all-failed-ids)
  - [Printing summary statistics for a run](#printing-summary-statistics)

## Extract all Successfully Downloaded IDs

This script is contained [here](extract_successful_ids.sh) and will result in a file that contains the IDs of everything that was successfully downloaded without an error. That is, a list will be created of submissions that, with the `--exclude-id-file` option, can be used so that the BDFR will not attempt to redownload these submissions/comments. This is likely to cause a performance increase, especially when the BDFR run finds many resources.

The script can be used with the following signature:

```bash
./extract_successful_ids.sh LOGFILE_LOCATION <OUTPUT_FILE>
```

By default, if the second argument is not supplied, the script will write the results to `successful.txt`.

An example of the script being run on a Linux machine is the following:

```bash
./extract_successful_ids.sh ~/.config/bdfr/log_output.txt
```

## Extract all Failed IDs

[This script](extract_failed_ids.sh) will output a file of all IDs that failed to be downloaded from the logfile in question. This may be used to prevent subsequent runs of the BDFR from re-attempting those submissions if that is desired, potentially increasing performance.
The script can be used with the following signature:

```bash
./extract_failed_ids.sh LOGFILE_LOCATION <OUTPUT_FILE>
```

By default, if the second argument is not supplied, the script will write the results to `failed.txt`.

An example of the script being run on a Linux machine is the following:

```bash
./extract_failed_ids.sh ~/.config/bdfr/log_output.txt
```

## Printing Summary Statistics

A simple script has been included to print summary statistics for a run of the BDFR. This is mainly to showcase how easy it is to extract statistics from the logfiles. You can extend this quite easily. For example, you can print how often the Imgur module is used, or how many 404 errors there are in the last run, or which module has caused the most errors. The possibilities really are endless.

```bash
./print_summary.sh LOGFILE_LOCATION
```

This will create an output like the following:

```text
Downloaded submissions: 250
Failed downloads: 103
Files already downloaded: 20073
Hard linked submissions: 30
Excluded submissions: 1146
Files with existing hash skipped: 0
Submissions from excluded subreddits: 0
```
