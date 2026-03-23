#!/usr/bin/env python3
import sys

# Mapping from AcadNusx/AAcadHN (Latin) to Georgian Unicode
# This corresponds to the standard "Georgian QWERTY" layout used by these old fonts.
TRANS_TABLE = {
    'a': 'ა', 'b': 'ბ', 'g': 'გ', 'd': 'დ', 'e': 'ე', 'v': 'ვ', 'z': 'ზ', 'T': 'თ',
    'i': 'ი', 'k': 'კ', 'l': 'ლ', 'm': 'მ', 'n': 'ნ', 'o': 'ო', 'p': 'პ', 'J': 'ჟ',
    'r': 'რ', 's': 'ს', 't': 'ტ', 'u': 'უ', 'f': 'ფ', 'q': 'ქ', 'R': 'ღ', 'y': 'ყ',
    'S': 'შ', 'C': 'ჩ', 'c': 'ც', 'Z': 'ძ', 'w': 'წ', 'W': 'ჭ', 'x': 'ხ', 'j': 'ჯ',
    'h': 'ჰ'
}

def convert_text(text):
    result = []
    for char in text:
        # If the character is in our table, replace it. Otherwise keep it as is.
        result.append(TRANS_TABLE.get(char, char))
    return "".join(result)

def main():
    print("Вставьте текст (AAcadHN) и нажмите Ctrl+D (Linux/Mac) или Ctrl+Z + Enter (Windows) для завершения:")
    try:
        input_text = sys.stdin.read()
        converted = convert_text(input_text)
        print("\n--- Результат (Unicode) ---\n")
        print(converted)
        print("\n---------------------------\n")
    except KeyboardInterrupt:
        print("\nОтменено.")

if __name__ == "__main__":
    main()
