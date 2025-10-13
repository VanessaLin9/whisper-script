# cli.py
import click

@click.group()
def cli():
    """Meeting transcription toolkit"""
    pass

@cli.command()
@click.argument('audio_file')
def preprocess(audio_file):
    """Split audio at silence points"""
    from src.preprocessing import audio_splitter
    audio_splitter.process(audio_file)

@cli.command()
@click.argument('input_dir')
def batch_transcribe(input_dir):
    """Batch transcribe multilingual meetings"""
    from pipelines.multilang_batch import run
    run(input_dir)

@cli.command()
def setup():
    """Initialize environment for new machine"""
    from src.utils.env_setup import setup_environment
    setup_environment()

if __name__ == '__main__':
    cli()