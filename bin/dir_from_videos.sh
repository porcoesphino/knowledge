#!/bin/bash

echo $1
echo $2

if [ ! -d "$1" ]; then
  echo "The first argment, source directory, was not a directory: $1"
  exit 1
fi

if [ ! -d "$2" ]; then
  echo "The second argument, target directory, is not a directory: $2"
  TARGET_DIR="${PWD}"
  echo "Using the current directory as the target: $TARGET_DIR"
else
  TARGET_DIR="${2}"
fi

MODIFIED_FILE_NAMES=$(ls -1 "$1"/S01E[0-9]*.webp | sed "s/\[.*//g"  | sed "s/ --.*//g" | sed "s/[']//g" | sed "s/\: / - /g" | sed "s/&/and/g" | sed "s/：/ -/g")
MODIFIED_FILE_NAMES=$(echo "${MODIFIED_FILE_NAMES}" | sed "s#^$1##g" | sed "s#^/##g")

echo "About to create these directories:"
echo "${MODIFIED_FILE_NAMES}"
echo ""
echo "In this target directory: ${TARGET_DIR}"
echo ""
echo "Do you wish to continue?"
select yn in "Yes" "No"; do
    case $yn in
        Yes ) break;;
        No ) exit 1;;
    esac
done

echo "${MODIFIED_FILE_NAMES}" | xargs -L 1 -I{} mkdir "${TARGET_DIR}"/'{}'

echo ""
echo "Done"
