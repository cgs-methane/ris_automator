import requests
import os

def reconstruct_abstract(inverted_index):
    """
    Reconstruct the abstract text from an inverted index.
    The inverted index is a dict where keys are words 
    and values are lists of positions.
    """
    # Gather all positions to determine abstract length.
    positions = []
    for word, indices in inverted_index.items():
        positions.extend(indices)
    if not positions:
        return ""
    n_words = max(positions) + 1
    # Create a list to hold the words in order.
    abstract_words = [""] * n_words
    for word, indices in inverted_index.items():
        for index in indices:
            abstract_words[index] = word
    return " ".join(abstract_words)

def create_ris_entry(title, authors, year, doi, abstract):
    """
    Build an RIS formatted string including abstract.
    RIS fields:
      TY  - Type (JOUR for journal article)
      TI  - Title
      AU  - Author (multiple AU fields if more than one author)
      PY  - Publication Year
      DO  - DOI
      AB  - Abstract
      ER  - End record
    """
    ris_lines = []
    ris_lines.append("TY  - JOUR")
    ris_lines.append(f"TI  - {title}")
    for author in authors:
        ris_lines.append(f"AU  - {author}")
    if year:
        ris_lines.append(f"PY  - {year}")
    if doi:
        ris_lines.append(f"DO  - {doi}")
    if abstract:
        ris_lines.append(f"AB  - {abstract}")
    ris_lines.append("ER  -")
    return "\n".join(ris_lines)


def main():
    """
    Example usage:
    This ‘main’ function just demonstrates fetching
    a single article’s metadata and creating an RIS file.
    """
    # 1. Define the article title to search for.
    article_title = "New technologies can cost effectively reduce oil and gas methane emissions"
    search_url = "https://api.openalex.org/works"
    params = {"search": article_title}
    
    try:
        response = requests.get(search_url, params=params)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error during OpenAlex search request: {e}")
        return
    
    data = response.json()
    if "results" not in data or len(data["results"]) == 0:
        print("No results found for the article name.")
        return
    
    # 2. Get the first (most relevant) result.
    first_result = data["results"][0]
    title = first_result.get("display_name", "No Title")
    doi = first_result.get("doi", None)
    year = first_result.get("publication_year", "")
    
    # 3. Extract authors
    authors = []
    for authorship in first_result.get("authorships", []):
        author_info = authorship.get("author", {})
        author_name = author_info.get("display_name")
        if author_name:
            authors.append(author_name)
    
    # 4. Reconstruct abstract if available
    abstract = ""
    if "abstract_inverted_index" in first_result and first_result["abstract_inverted_index"]:
        abstract = reconstruct_abstract(first_result["abstract_inverted_index"])
    
    # Log the found details
    print("Found article:")
    print(f"Title: {title}")
    print(f"DOI: {doi}")
    print(f"Publication Year: {year}")
    print(f"Authors: {', '.join(authors) if authors else 'N/A'}")
    if abstract:
        print("Abstract successfully reconstructed.")
    else:
        print("No abstract available.")

    # 5. Create an RIS formatted string
    ris_content = create_ris_entry(title, authors, year, doi, abstract)
    
    # 6. Save the RIS file
    filename = "article_with_abstract.ris"
    try:
        with open(filename, "w", encoding="utf-8") as file:
            file.write(ris_content)
        print(f"RIS file successfully saved as '{filename}'.")
    except Exception as e:
        print(f"Error saving RIS file: {e}")
        return
    
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        print("RIS file verified (file is not empty).")
    else:
        print("RIS file appears to be empty or missing.")


if __name__ == "__main__":
    main()
